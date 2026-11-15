"""Lab: OAuth scopes vs RBAC vs ABAC vs Zanzibar ReBAC on one document-sharing scenario."""

import httpx
import pytest
from authlib.oauth2.rfc6749.resource_protector import (  # type: ignore[import-untyped]
    TokenValidator,
)
from authlib.oauth2.rfc6749.util import scope_to_list  # type: ignore[import-untyped]

from api_labs.f15_api_authorization_and_owasp_security.authorization_models import (
    DOC_ABAC_RULES,
    DOCS_CONFIG,
    AccessRequest,
    ComputedUserset,
    Decision,
    Document,
    Exclusion,
    ModelRow,
    Permission,
    Principal,
    RbacConfigError,
    RbacPolicy,
    Rule,
    ScopeSyntaxError,
    SeparationOfDutyError,
    This,
    TupleStore,
    Userset,
    Zanzibar,
    Zookie,
    build_docs_app,
    decide_all,
    deny_overrides,
    insufficient_scope_challenge,
    parse_scope,
    parse_tuple,
    pep_allows,
    permit_overrides,
)

ROADMAP = Document("roadmap", tenant="acme", owner="alice", classification=2)


def docs_store(replica_lag: int = 0) -> TupleStore:
    store = TupleStore(replica_lag)
    store.write(
        "doc:roadmap#owner@alice",
        "doc:roadmap#parent@folder:eng#...",
        "folder:eng#viewer@group:eng#member",
        "group:eng#member@bob",
        "group:eng#member@erin",
        "doc:roadmap#banned@erin",
    )
    return store


# --- scopes ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scope_grammar_is_strict_space_delimited_and_case_sensitive() -> None:
    assert parse_scope("docs:read docs:write") == {"docs:read", "docs:write"}
    assert "docs:read" not in parse_scope("Docs:Read")
    for bad in ("", "docs:read  docs:write", "docs:read\tdocs:write", 'a"b', "a\\b", " docs"):
        with pytest.raises(ScopeSyntaxError):
            parse_scope(bad)


@pytest.mark.unit
def test_authlib_scope_parsing_is_lenient_and_required_list_is_an_or() -> None:
    # str.split() accepts the TAB and the double space that RFC 6749 Section 3.3 forbids.
    lenient = scope_to_list("docs:read\tdocs:write  admin")  # pyright: ignore[reportUnknownVariableType]
    assert lenient == ["docs:read", "docs:write", "admin"]
    insufficient = TokenValidator.scope_insufficient  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    # Each list element is an alternative; spaces inside one element mean AND.
    assert insufficient("docs:read", ["docs:read docs:write", "admin"]) is True
    assert insufficient("admin", ["docs:read docs:write", "admin"]) is False
    assert insufficient("docs:write docs:read", ["docs:read docs:write"]) is False


@pytest.mark.unit
def test_insufficient_scope_challenge_names_every_required_scope() -> None:
    challenge = insufficient_scope_challenge(["docs:write", "docs:read"])
    assert challenge == 'Bearer error="insufficient_scope", scope="docs:write docs:read"'


# --- RBAC ---------------------------------------------------------------------------------


@pytest.mark.unit
def test_rbac_senior_role_inherits_junior_permissions_but_not_the_reverse() -> None:
    policy = RbacPolicy(
        role_permissions={
            "viewer": frozenset({Permission("read", "document")}),
            "editor": frozenset({Permission("write", "document")}),
        },
        inherits={"editor": frozenset({"viewer"})},
    )
    assert policy.effective_roles(["editor"]) == {"editor", "viewer"}
    assert policy.permits(["editor"], "read", "document")
    assert not policy.permits(["viewer"], "write", "document")


@pytest.mark.unit
def test_rbac_rejects_hierarchy_cycles_and_unknown_roles() -> None:
    perms = {"a": frozenset[Permission](), "b": frozenset[Permission]()}
    with pytest.raises(RbacConfigError, match="cycle: a -> b -> a"):
        RbacPolicy(perms, inherits={"a": frozenset({"b"}), "b": frozenset({"a"})})
    with pytest.raises(RbacConfigError, match="unknown roles"):
        RbacPolicy(perms, inherits={"a": frozenset({"ghost"})})


@pytest.mark.unit
def test_rbac_static_separation_of_duty_counts_inherited_roles() -> None:
    policy = RbacPolicy(
        role_permissions={
            "approver": frozenset({Permission("approve", "payment")}),
            "requester": frozenset({Permission("create", "payment")}),
            "finance_lead": frozenset[Permission](),
        },
        inherits={"finance_lead": frozenset({"approver"})},
        ssd=((frozenset({"approver", "requester"}), 2),),
    )
    policy.check_assignment(["approver"])
    policy.check_assignment(["finance_lead"])
    with pytest.raises(SeparationOfDutyError, match=r"\['approver', 'requester'\]"):
        policy.check_assignment(["finance_lead", "requester"])


# --- ABAC ---------------------------------------------------------------------------------


def _request(subject: dict[str, str | int], action: str = "read") -> AccessRequest:
    return AccessRequest(
        subject=subject,
        resource={"tenant": "acme", "owner": "alice", "classification": 2},
        action=action,
    )


@pytest.mark.unit
def test_abac_combining_algorithm_decides_a_deny_permit_conflict() -> None:
    # alice still owns the document but her account moved to another tenant
    moved = _request({"id": "alice", "tenant": "globex", "clearance": 3})
    assert deny_overrides(DOC_ABAC_RULES, moved)[0] is Decision.DENY
    assert permit_overrides(DOC_ABAC_RULES, moved)[0] is Decision.PERMIT
    # XACML 3.0 C.4: a Deny beats an Indeterminate{D}, but not an Indeterminate{P}.
    broken_deny = Rule("deny-ghost", Decision.DENY, lambda r: bool(r.attr("subject", "x")))
    broken_permit = Rule("permit-ghost", Decision.PERMIT, lambda r: bool(r.attr("subject", "x")))
    always_deny = Rule("deny-all", Decision.DENY, lambda r: True)
    assert permit_overrides((broken_deny, always_deny), moved)[0] is Decision.DENY
    assert permit_overrides((broken_permit, always_deny), moved)[0] is Decision.INDETERMINATE
    assert deny_overrides((broken_permit, always_deny), moved)[0] is Decision.DENY


@pytest.mark.unit
def test_abac_missing_attribute_in_a_deny_rule_fails_closed() -> None:
    no_tenant = _request({"id": "alice", "clearance": 3})
    decision, results = deny_overrides(DOC_ABAC_RULES, no_tenant)
    assert [(r.rule_id, r.decision) for r in results] == [
        ("deny-cross-tenant", Decision.INDETERMINATE),
        ("permit-read-cleared", Decision.PERMIT),
        ("permit-owner", Decision.PERMIT),
    ]
    assert decision is Decision.INDETERMINATE
    assert not pep_allows(decision)
    # The same request under permit-overrides is let through by the two Permit rules.
    assert pep_allows(permit_overrides(DOC_ABAC_RULES, no_tenant)[0])


# --- ReBAC ----------------------------------------------------------------------------------


@pytest.mark.unit
def test_tuple_text_notation_parses_users_and_usersets() -> None:
    plain = parse_tuple("doc:readme#owner@10")
    nested = parse_tuple("doc:readme#viewer@group:eng#member")
    assert plain.subject == "10"
    assert isinstance(nested.subject, Userset)
    assert str(nested.subject) == "group:eng#member"
    assert str(nested) == "doc:readme#viewer@group:eng#member"
    for bad in ("doc:readme#owner", "readme#owner@10", "doc:readme@10"):
        with pytest.raises(ValueError, match=r"tuple|object"):
            parse_tuple(bad)


@pytest.mark.unit
def test_rebac_concentric_relations_folder_inheritance_and_exclusion() -> None:
    engine = Zanzibar(DOCS_CONFIG, docs_store())
    assert engine.check("doc:roadmap", "editor", "alice").allowed  # owner -> editor
    assert engine.check("doc:roadmap", "viewer", "alice").allowed  # editor -> viewer
    assert engine.check("doc:roadmap", "viewer", "bob").allowed  # group -> folder -> doc
    assert not engine.check("doc:roadmap", "editor", "bob").allowed
    assert engine.check("doc:roadmap", "viewer", "erin").allowed
    assert not engine.check("doc:roadmap", "can_view", "erin").allowed  # viewer - banned
    assert not engine.check("doc:roadmap", "viewer", "dave").allowed


@pytest.mark.unit
def test_new_enemy_problem_and_the_zookie_that_prevents_it() -> None:
    # Zanzibar Section 2.2, Example B: remove Bob, then add new content to the document.
    store = TupleStore(replica_lag=1)
    engine = Zanzibar(DOCS_CONFIG, store)
    store.write("doc:readme#viewer@bob", "doc:readme#editor@charlie")
    store.delete("doc:readme#viewer@bob")  # 1. Alice removes Bob from the ACL
    allowed, zookie = engine.content_change_check("doc:readme", "editor", "charlie")
    assert allowed.allowed  # 2. Charlie adds content; the zookie is stored with it
    assert zookie == Zookie(2)
    stale = engine.check("doc:readme", "viewer", "bob")  # 3. no zookie: replica snapshot
    assert (stale.allowed, stale.revision) == (True, 1)  # the new enemy sees new content
    fresh = engine.check("doc:readme", "viewer", "bob", at_least=zookie)
    assert (fresh.allowed, fresh.revision) == (False, 2)


@pytest.mark.unit
def test_group_cycle_terminates_in_a_union_and_fails_closed_under_exclusion() -> None:
    store = docs_store()
    store.write(
        "group:a#member@group:b#member",
        "group:b#member@group:a#member",
        "group:b#member@bob",
        "doc:x#viewer@zed",
        "doc:x#banned@group:a#member",
    )
    engine = Zanzibar(DOCS_CONFIG, store)
    assert engine.check("group:a", "member", "bob").allowed
    assert not engine.check("group:a", "member", "zed").allowed  # cycle, no exception
    result = engine.check("doc:x", "can_view", "zed")
    assert not result.allowed
    assert "cycle at group:a#member under an exclusion" in result.reason


@pytest.mark.unit
def test_nested_group_chain_hits_the_depth_budget_and_counts_reads() -> None:
    store = TupleStore()
    store.write(*(f"group:g{i}#member@group:g{i + 1}#member" for i in range(30)))
    store.write("group:g30#member@u")
    deep = Zanzibar(DOCS_CONFIG, store, max_depth=40).check("group:g0", "member", "u")
    assert deep.allowed
    assert store.reads == 31  # one read per nested group
    store.reads = 0
    capped = Zanzibar(DOCS_CONFIG, store).check("group:g0", "member", "u")
    assert not capped.allowed
    assert capped.reason == "denied, fail closed: depth 26 exceeds 25 at group:g26#member"
    assert store.reads == 26


@pytest.mark.unit
def test_unknown_relation_is_a_configuration_error_not_a_deny() -> None:
    engine = Zanzibar({"doc": {"viewer": Exclusion(This(), ComputedUserset("nope"))}}, TupleStore())
    engine.store.write("doc:a#viewer@u")
    with pytest.raises(KeyError, match="'nope' not defined for namespace 'doc'"):
        engine.check("doc:a", "viewer", "u")


# --- one question, four models ------------------------------------------------------------


@pytest.mark.unit
def test_four_models_answer_the_same_question_differently() -> None:
    engine = Zanzibar(DOCS_CONFIG, docs_store())
    editor, viewer = frozenset({"editor"}), frozenset({"viewer"})
    cases = [
        ("owner", Principal("alice", "acme", editor, "docs:read"), 3),
        ("shared via group and folder", Principal("bob", "acme", viewer, "docs:read"), 1),
        ("same tenant, never shared", Principal("dave", "acme", viewer, "docs:read"), 3),
        ("other tenant", Principal("mallory", "globex", viewer, "docs:read"), 3),
        ("shared, then banned", Principal("erin", "acme", viewer, "docs:read"), 3),
        ("owner, token lacks docs:read", Principal("alice", "acme", editor, "profile"), 3),
    ]
    rows = [decide_all(name, who, ROADMAP, clearance, engine) for name, who, clearance in cases]
    assert rows == [
        ModelRow("owner", scopes=True, rbac=True, abac=True, rebac=True),
        ModelRow("shared via group and folder", scopes=True, rbac=True, abac=False, rebac=True),
        ModelRow("same tenant, never shared", scopes=True, rbac=True, abac=True, rebac=False),
        ModelRow("other tenant", scopes=True, rbac=True, abac=False, rebac=False),
        ModelRow("shared, then banned", scopes=True, rbac=True, abac=True, rebac=False),
        ModelRow("owner, token lacks docs:read", scopes=False, rbac=True, abac=True, rebac=True),
    ]


# --- FastAPI layering: scope at the edge, relationship at the object ----------------------

TOKENS = {
    "t-alice-rw": ("alice", "docs:read docs:write"),
    "t-alice-w": ("alice", "docs:write"),
    "t-alice-profile": ("alice", "profile"),
    "t-bob-rw": ("bob", "docs:read docs:write"),
    "t-dave-rw": ("dave", "docs:read docs:write"),
}


def _client() -> httpx.AsyncClient:
    app = build_docs_app(Zanzibar(DOCS_CONFIG, docs_store()), TOKENS)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_codes_separate_authentication_scope_and_object_denials() -> None:
    async with _client() as client:
        missing = await client.get("/docs/roadmap")
        unknown = await client.get("/docs/roadmap", headers=_auth("t-forged"))
        no_scope = await client.get("/docs/roadmap", headers=_auth("t-alice-profile"))
        shared = await client.get("/docs/roadmap", headers=_auth("t-bob-rw"))
        unshared = await client.get("/docs/roadmap", headers=_auth("t-dave-rw"))
    assert (missing.status_code, missing.headers["www-authenticate"]) == (401, "Bearer")
    assert unknown.status_code == 401
    assert unknown.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert no_scope.status_code == 403
    assert no_scope.headers["www-authenticate"] == (
        'Bearer error="insufficient_scope", scope="docs:read"'
    )
    assert (shared.status_code, shared.json()) == (200, {"doc_id": "roadmap", "user": "bob"})
    assert unshared.status_code == 404  # hide existence (RFC 9110 Section 15.5.4)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_security_scopes_accumulate_along_the_dependency_chain() -> None:
    async with _client() as client:
        write_only = await client.patch("/docs/roadmap", headers=_auth("t-alice-w"))
        owner = await client.patch("/docs/roadmap", headers=_auth("t-alice-rw"))
        viewer = await client.patch("/docs/roadmap", headers=_auth("t-bob-rw"))
        stranger = await client.patch("/docs/roadmap", headers=_auth("t-dave-rw"))
    # The route asks for docs:write; its reader dependency adds docs:read.
    assert write_only.status_code == 403
    assert write_only.headers["www-authenticate"] == (
        'Bearer error="insufficient_scope", scope="docs:write docs:read"'
    )
    assert owner.status_code == 200
    assert viewer.status_code == 403  # may see it, may not edit it
    assert "www-authenticate" not in viewer.headers
    assert stranger.status_code == 404
