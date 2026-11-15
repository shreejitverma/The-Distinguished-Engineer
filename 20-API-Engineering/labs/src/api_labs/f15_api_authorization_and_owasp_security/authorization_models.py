"""Four authorization models on one scenario: OAuth scopes, RBAC, ABAC, and Zanzibar ReBAC."""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes

# --- 1. OAuth 2.0 scopes (RFC 6749 Section 3.3, RFC 6750 Section 3.1) -------------------

# scope-token = 1*( %x21 / %x23-5B / %x5D-7E ): printable ASCII except SP, DQUOTE, and "\"
_SCOPE_TOKEN = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+")


class ScopeSyntaxError(ValueError):
    """The value does not match scope = scope-token *( SP scope-token )."""


def parse_scope(value: str) -> frozenset[str]:
    """Strict RFC 6749 Section 3.3 parse: single-SP delimited, case-sensitive, non-empty."""
    tokens = value.split(" ")
    for token in tokens:
        if not _SCOPE_TOKEN.fullmatch(token):
            raise ScopeSyntaxError(f"bad scope-token {token!r} in {value!r}")
    return frozenset(tokens)


def missing_scopes(granted: frozenset[str], required: Iterable[str]) -> tuple[str, ...]:
    """Required scopes absent from the token, in the order they were required."""
    return tuple(scope for scope in required if scope not in granted)


def insufficient_scope_challenge(required: Iterable[str]) -> str:
    """WWW-Authenticate value for a 403 insufficient_scope response (RFC 6750 Section 3)."""
    return f'Bearer error="insufficient_scope", scope="{" ".join(required)}"'


# --- 2. RBAC: NIST core, hierarchical, and static separation of duty ---------------------


@dataclass(frozen=True, slots=True, order=True)
class Permission:
    """A permission is an operation on an object type; RBAC never names one object."""

    action: str
    resource_type: str


class RbacConfigError(ValueError):
    """Unknown role, or a cycle in the role hierarchy."""


class SeparationOfDutyError(ValueError):
    """An assignment puts a user in too many roles of a conflicting set."""


@dataclass(frozen=True, slots=True)
class RbacPolicy:
    """role -> permissions, senior role -> junior roles, and SSD constraints (roles, n)."""

    role_permissions: Mapping[str, frozenset[Permission]]
    inherits: Mapping[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    ssd: tuple[tuple[frozenset[str], int], ...] = ()

    def __post_init__(self) -> None:
        roles = set(self.role_permissions)
        for senior, juniors in self.inherits.items():
            unknown = ({senior} | juniors) - roles
            if unknown:
                raise RbacConfigError(f"unknown roles in hierarchy: {sorted(unknown)}")
        for role in self.role_permissions:  # dict order: deterministic cycle report
            self._closure(role, ())

    def _closure(self, role: str, path: tuple[str, ...]) -> frozenset[str]:
        if role in path:
            raise RbacConfigError(f"role hierarchy cycle: {' -> '.join((*path, role))}")
        result = {role}
        for junior in self.inherits.get(role, frozenset()):
            result |= self._closure(junior, (*path, role))
        return frozenset(result)

    def effective_roles(self, assigned: Iterable[str]) -> frozenset[str]:
        """Assigned roles plus every junior role they inherit (a partial order)."""
        out: set[str] = set()
        for role in assigned:
            if role not in self.role_permissions:
                raise RbacConfigError(f"unknown role {role!r}")
            out |= self._closure(role, ())
        return frozenset(out)

    def check_assignment(self, assigned: Iterable[str]) -> None:
        """SSD with hierarchies: inherited roles count toward the limit."""
        effective = self.effective_roles(assigned)
        for conflicting, limit in self.ssd:
            held = sorted(effective & conflicting)
            if len(held) >= limit:
                raise SeparationOfDutyError(f"SSD violated: {held} reaches limit {limit}")

    def permits(self, assigned: Iterable[str], action: str, resource_type: str) -> bool:
        wanted = Permission(action, resource_type)
        return any(wanted in self.role_permissions[r] for r in self.effective_roles(assigned))


# --- 3. ABAC: rules over subject, resource, action, and environment attributes -----------


class Decision(StrEnum):
    PERMIT = "Permit"
    DENY = "Deny"
    NOT_APPLICABLE = "NotApplicable"
    INDETERMINATE = "Indeterminate"


class MissingAttributeError(LookupError):
    """A rule needed an attribute the request does not carry (MustBePresent semantics)."""


type AttrValue = str | int | bool | frozenset[str]


@dataclass(frozen=True, slots=True)
class AccessRequest:
    subject: Mapping[str, AttrValue]
    resource: Mapping[str, AttrValue]
    action: str
    environment: Mapping[str, AttrValue] = field(default_factory=dict[str, AttrValue])

    def attr(self, category: Literal["subject", "resource", "environment"], name: str) -> AttrValue:
        bag: Mapping[str, AttrValue] = getattr(self, category)
        if name not in bag:
            raise MissingAttributeError(f"{category}.{name}")
        return bag[name]


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    effect: Literal[Decision.PERMIT, Decision.DENY]
    condition: Callable[[AccessRequest], bool]


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    decision: Decision
    reason: str = ""


def evaluate_rule(rule: Rule, request: AccessRequest) -> RuleResult:
    try:
        matched = rule.condition(request)
    except MissingAttributeError as exc:
        return RuleResult(rule.rule_id, Decision.INDETERMINATE, f"missing {exc.args[0]}")
    return RuleResult(rule.rule_id, rule.effect if matched else Decision.NOT_APPLICABLE)


def deny_overrides(
    rules: Iterable[Rule], request: AccessRequest
) -> tuple[Decision, list[RuleResult]]:
    """Any Deny wins; an error in a Deny rule is treated as a possible Deny (fail closed).

    XACML 3.0 Appendix C.2 with the extended Indeterminate values collapsed into one.
    """
    rule_list = tuple(rules)
    results = [evaluate_rule(rule, request) for rule in rule_list]
    decisions = {r.decision for r in results}
    if Decision.DENY in decisions:
        return Decision.DENY, results
    if any(
        result.decision is Decision.INDETERMINATE and rule.effect is Decision.DENY
        for result, rule in zip(results, rule_list, strict=True)
    ):
        return Decision.INDETERMINATE, results
    if Decision.PERMIT in decisions:
        return Decision.PERMIT, results
    if Decision.INDETERMINATE in decisions:
        return Decision.INDETERMINATE, results
    return Decision.NOT_APPLICABLE, results


def permit_overrides(
    rules: Iterable[Rule], request: AccessRequest
) -> tuple[Decision, list[RuleResult]]:
    """Any Permit wins; an error in a Permit rule is treated as a possible Permit.

    XACML 3.0 Appendix C.4 with the extended Indeterminate values collapsed into one: a Deny
    beats an Indeterminate that could only have been a Deny.
    """
    rule_list = tuple(rules)
    results = [evaluate_rule(rule, request) for rule in rule_list]
    decisions = {r.decision for r in results}
    if Decision.PERMIT in decisions:
        return Decision.PERMIT, results
    if any(
        result.decision is Decision.INDETERMINATE and rule.effect is Decision.PERMIT
        for result, rule in zip(results, rule_list, strict=True)
    ):
        return Decision.INDETERMINATE, results
    if Decision.DENY in decisions:
        return Decision.DENY, results
    if Decision.INDETERMINATE in decisions:
        return Decision.INDETERMINATE, results
    return Decision.NOT_APPLICABLE, results


def pep_allows(decision: Decision) -> bool:
    """A deny-biased enforcement point: only an explicit Permit lets the request through."""
    return decision is Decision.PERMIT


# --- 4. ReBAC: Zanzibar relation tuples, userset rewrites, snapshots, and zookies --------


@dataclass(frozen=True, slots=True, order=True)
class ObjectRef:
    namespace: str
    object_id: str

    def __str__(self) -> str:
        return f"{self.namespace}:{self.object_id}"


@dataclass(frozen=True, slots=True, order=True)
class Userset:
    obj: ObjectRef
    relation: str  # "..." means "the object itself", used by tuple_to_userset parents

    def __str__(self) -> str:
        return f"{self.obj}#{self.relation}"


type Subject = str | Userset  # a user id, or a set of users defined by another object#relation


@dataclass(frozen=True, slots=True, order=True)
class RelationTuple:
    obj: ObjectRef
    relation: str
    subject: Subject

    def __str__(self) -> str:
        return f"{self.obj}#{self.relation}@{self.subject}"


def _object_ref(text: str) -> ObjectRef:
    namespace, sep, object_id = text.partition(":")
    if not sep or not namespace or not object_id:
        raise ValueError(f"object must be namespace:object_id, got {text!r}")
    return ObjectRef(namespace, object_id)


def parse_tuple(text: str) -> RelationTuple:
    """Zanzibar text notation: <object>'#'<relation>'@'<user>, user = id | <object>'#'<relation>."""
    lhs, at, user = text.partition("@")
    obj, hash_, relation = lhs.partition("#")
    if not at or not hash_ or not relation or not user:
        raise ValueError(f"not a relation tuple: {text!r}")
    subject: Subject = user
    if "#" in user:
        user_obj, _, user_rel = user.partition("#")
        subject = Userset(_object_ref(user_obj), user_rel)
    return RelationTuple(_object_ref(obj), relation, subject)


# A userset rewrite is an expression tree; the alias is lazy, so it may name later classes.
type Rewrite = This | ComputedUserset | TupleToUserset | Union | Intersection | Exclusion
type NamespaceConfig = Mapping[str, Mapping[str, Rewrite]]


@dataclass(frozen=True, slots=True)
class This:
    """_this: users named by stored tuples for <object#relation>, following usersets."""


@dataclass(frozen=True, slots=True)
class ComputedUserset:
    relation: str  # the same object, another relation


@dataclass(frozen=True, slots=True)
class TupleToUserset:
    tupleset: str  # relation on this object whose tuples point at other objects
    computed_relation: str  # relation to evaluate on each object those tuples point at


@dataclass(frozen=True, slots=True)
class Union:
    children: tuple[Rewrite, ...]


@dataclass(frozen=True, slots=True)
class Intersection:
    children: tuple[Rewrite, ...]


@dataclass(frozen=True, slots=True)
class Exclusion:
    base: Rewrite
    subtract: Rewrite


@dataclass(frozen=True, slots=True, order=True)
class Zookie:
    """Snapshot lower bound. Zanzibar's zookie is opaque bytes; the revision is shown here."""

    revision: int


@dataclass(slots=True)
class _Version:
    tup: RelationTuple
    written: int
    deleted: int | None = None


class TupleStore:
    """Multi-version tuple storage: every write gets a revision, reads are snapshot reads.

    ``replica_lag`` models a replica that has applied every write except the last N, which is
    the snapshot a check gets when the caller sends no zookie.
    """

    def __init__(self, replica_lag: int = 0) -> None:
        self._versions: list[_Version] = []
        self.head = 0
        self.replica_lag = replica_lag
        self.reads = 0

    def write(self, *tuples: str) -> Zookie:
        self.head += 1
        for text in tuples:
            self._versions.append(_Version(parse_tuple(text), self.head))
        return Zookie(self.head)

    def delete(self, *tuples: str) -> Zookie:
        self.head += 1
        wanted = {parse_tuple(text) for text in tuples}
        for version in self._versions:
            if version.tup in wanted and version.deleted is None:
                version.deleted = self.head
        return Zookie(self.head)

    def snapshot(self, at_least: Zookie | None) -> int:
        """At-least-as-fresh: the replica snapshot, unless the zookie demands a newer one."""
        replica = max(0, self.head - self.replica_lag)
        return replica if at_least is None else max(replica, min(at_least.revision, self.head))

    def read(self, obj: ObjectRef, relation: str, revision: int) -> list[Subject]:
        self.reads += 1
        return [
            v.tup.subject
            for v in self._versions
            if v.tup.obj == obj
            and v.tup.relation == relation
            and v.written <= revision
            and (v.deleted is None or v.deleted > revision)
        ]


class ResolutionLimitError(RuntimeError):
    """The check needed more nested resolutions than the depth budget allows."""


class IndeterminateCheckError(RuntimeError):
    """A cycle was reached under an exclusion, where 'no path' would mean 'grant'."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    allowed: bool
    revision: int
    reason: str = ""


class Zanzibar:
    """Check over a TupleStore with per-namespace userset rewrite rules."""

    def __init__(self, config: NamespaceConfig, store: TupleStore, max_depth: int = 25) -> None:
        self.config = config
        self.store = store
        self.max_depth = max_depth

    def check(
        self, obj: str, relation: str, user: str, at_least: Zookie | None = None
    ) -> CheckResult:
        revision = self.store.snapshot(at_least)
        try:
            allowed = self._check(_object_ref(obj), relation, user, revision, 0, frozenset(), False)
        except (ResolutionLimitError, IndeterminateCheckError) as exc:
            return CheckResult(False, revision, f"denied, fail closed: {exc}")
        return CheckResult(allowed, revision)

    def content_change_check(
        self, obj: str, relation: str, user: str
    ) -> tuple[CheckResult, Zookie]:
        """Evaluate at the latest snapshot and hand back a zookie to store with the content."""
        result = self.check(obj, relation, user, Zookie(self.store.head))
        return result, Zookie(result.revision)

    def _rewrite(self, obj: ObjectRef, relation: str) -> Rewrite:
        try:
            return self.config[obj.namespace][relation]
        except KeyError:
            raise KeyError(
                f"relation {relation!r} not defined for namespace {obj.namespace!r}"
            ) from None

    def _check(
        self,
        obj: ObjectRef,
        relation: str,
        user: str,
        revision: int,
        depth: int,
        path: frozenset[tuple[ObjectRef, str]],
        negated: bool,
    ) -> bool:
        if depth > self.max_depth:
            raise ResolutionLimitError(
                f"depth {depth} exceeds {self.max_depth} at {obj}#{relation}"
            )
        node = (obj, relation)
        if node in path:
            if negated:
                raise IndeterminateCheckError(f"cycle at {obj}#{relation} under an exclusion")
            return False  # a cycle adds no members to a union: least fixpoint
        path = path | {node}
        rewrite = self._rewrite(obj, relation)
        return self._eval(rewrite, obj, relation, user, revision, depth, path, negated)

    def _eval(
        self,
        rewrite: Rewrite,
        obj: ObjectRef,
        relation: str,
        user: str,
        revision: int,
        depth: int,
        path: frozenset[tuple[ObjectRef, str]],
        negated: bool,
    ) -> bool:
        match rewrite:
            case This():
                for subject in self.store.read(obj, relation, revision):
                    if isinstance(subject, str):
                        if subject == user:
                            return True
                    elif subject.relation != "..." and self._check(
                        subject.obj, subject.relation, user, revision, depth + 1, path, negated
                    ):
                        return True
                return False
            case ComputedUserset(relation=other):
                return self._check(obj, other, user, revision, depth + 1, path, negated)
            case TupleToUserset(tupleset=tupleset, computed_relation=computed):
                for subject in self.store.read(obj, tupleset, revision):
                    target = subject.obj if isinstance(subject, Userset) else None
                    if target is not None and self._check(
                        target, computed, user, revision, depth + 1, path, negated
                    ):
                        return True
                return False
            case Union(children=children):
                return any(
                    self._eval(c, obj, relation, user, revision, depth, path, negated)
                    for c in children
                )
            case Intersection(children=children):
                return all(
                    self._eval(c, obj, relation, user, revision, depth, path, negated)
                    for c in children
                )
            case Exclusion(base=base, subtract=subtract):
                if not self._eval(base, obj, relation, user, revision, depth, path, negated):
                    return False
                return not self._eval(subtract, obj, relation, user, revision, depth, path, True)


# Figure 1 of the Zanzibar paper (owner -> editor -> viewer, viewers of the parent folder are
# viewers), plus a "banned" exclusion and nested groups.
DOCS_CONFIG: NamespaceConfig = {
    "doc": {
        "owner": This(),
        "parent": This(),
        "banned": This(),
        "editor": Union((This(), ComputedUserset("owner"))),
        "viewer": Union((This(), ComputedUserset("editor"), TupleToUserset("parent", "viewer"))),
        "can_view": Exclusion(ComputedUserset("viewer"), ComputedUserset("banned")),
    },
    "folder": {
        "parent": This(),
        "viewer": Union((This(), TupleToUserset("parent", "viewer"))),
    },
    "group": {"member": This()},
}


# --- 5. One question, four models --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Principal:
    user: str
    tenant: str
    roles: frozenset[str]
    token_scope: str


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    tenant: str
    owner: str
    classification: int


DOC_RBAC = RbacPolicy(
    role_permissions={
        "viewer": frozenset({Permission("read", "document")}),
        "editor": frozenset({Permission("write", "document")}),
        "auditor": frozenset({Permission("read", "audit_log")}),
        "payments_approver": frozenset({Permission("approve", "payment")}),
        "payments_requester": frozenset({Permission("create", "payment")}),
    },
    inherits={"editor": frozenset({"viewer"})},
    ssd=((frozenset({"payments_approver", "payments_requester"}), 2),),
)


def _same_tenant(r: AccessRequest) -> bool:
    return r.attr("subject", "tenant") == r.attr("resource", "tenant")


def _cleared(r: AccessRequest) -> bool:
    clearance, classification = r.attr("subject", "clearance"), r.attr("resource", "classification")
    return (
        isinstance(clearance, int)
        and isinstance(classification, int)
        and clearance >= classification
    )


DOC_ABAC_RULES: tuple[Rule, ...] = (
    Rule("deny-cross-tenant", Decision.DENY, lambda r: not _same_tenant(r)),
    Rule("permit-read-cleared", Decision.PERMIT, lambda r: r.action == "read" and _cleared(r)),
    Rule(
        "permit-owner",
        Decision.PERMIT,
        lambda r: r.attr("resource", "owner") == r.attr("subject", "id"),
    ),
)


@dataclass(frozen=True, slots=True)
class ModelRow:
    scenario: str
    scopes: bool
    rbac: bool
    abac: bool
    rebac: bool


def decide_all(
    scenario: str,
    who: Principal,
    doc: Document,
    clearance: int,
    rebac: Zanzibar,
) -> ModelRow:
    """Ask 'may this principal read this document?' of each model in isolation."""
    by_scope = not missing_scopes(parse_scope(who.token_scope), ("docs:read",))
    by_role = DOC_RBAC.permits(who.roles, "read", "document")
    request = AccessRequest(
        subject={"id": who.user, "tenant": who.tenant, "clearance": clearance},
        resource={"tenant": doc.tenant, "owner": doc.owner, "classification": doc.classification},
        action="read",
    )
    by_attr = pep_allows(deny_overrides(DOC_ABAC_RULES, request)[0])
    by_relation = rebac.check(f"doc:{doc.doc_id}", "can_view", who.user).allowed
    return ModelRow(scenario, by_scope, by_role, by_attr, by_relation)


# --- 6. Layering in FastAPI: scopes at the edge, relationships at the object -------------


def build_docs_app(engine: Zanzibar, tokens: Mapping[str, tuple[str, str]]) -> FastAPI:
    """tokens maps an opaque bearer token to (user id, granted scope string)."""
    bearer = HTTPBearer()  # missing or non-Bearer Authorization -> 401 with WWW-Authenticate

    async def principal(
        security_scopes: SecurityScopes,
        creds: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    ) -> str:
        entry = tokens.get(creds.credentials)
        if entry is None:
            raise HTTPException(
                401, "invalid_token", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
            )
        user, scope = entry
        # security_scopes.scopes is every scope required along this dependency chain
        if missing_scopes(parse_scope(scope), security_scopes.scopes):
            challenge = insufficient_scope_challenge(security_scopes.scopes)
            raise HTTPException(403, "insufficient_scope", headers={"WWW-Authenticate": challenge})
        return user

    async def reader(user: Annotated[str, Security(principal, scopes=["docs:read"])]) -> str:
        return user

    app = FastAPI()

    @app.get("/docs/{doc_id}")
    async def read_doc(  # pyright: ignore[reportUnusedFunction]
        doc_id: str, user: Annotated[str, Security(principal, scopes=["docs:read"])]
    ) -> dict[str, str]:
        if not engine.check(f"doc:{doc_id}", "can_view", user).allowed:
            raise HTTPException(404, "Not Found")  # RFC 9110 Section 15.5.4: hide existence
        return {"doc_id": doc_id, "user": user}

    @app.patch("/docs/{doc_id}")
    async def edit_doc(  # pyright: ignore[reportUnusedFunction]
        doc_id: str, user: Annotated[str, Security(reader, scopes=["docs:write"])]
    ) -> dict[str, str]:
        if engine.check(f"doc:{doc_id}", "editor", user).allowed:
            return {"doc_id": doc_id, "editor": user}
        if engine.check(f"doc:{doc_id}", "can_view", user).allowed:
            raise HTTPException(403, "Forbidden")  # it may see the document, so 404 would lie
        raise HTTPException(404, "Not Found")

    return app
