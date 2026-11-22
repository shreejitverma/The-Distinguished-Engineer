"""Service mesh data planes modelled in-process: AuthorizationPolicy semantics at an Envoy
enforcement point and at an L4-only ztunnel, waypoint traversal, X.509 SVID rotation under CA
outage, node-scoped certificate issuance, HBONE tunnels over real HTTP/2 state machines, and the
sidecar versus ambient footprint arithmetic.

Every model cites the upstream file it mirrors (Istio and ztunnel 1.32.0).
Nothing here opens a socket or reads a clock; time is an explicit argument."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

import h2.config
import h2.connection
import h2.events
import h2.settings
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    import random
    from collections.abc import Callable, Sequence

# --------------------------------------------------------------------------------------------
# Identity (SPIFFE ID layout used by Istio: spiffe://<trust-domain>/ns/<ns>/sa/<sa>)
# --------------------------------------------------------------------------------------------

MAX_SPIFFE_ID_BYTES = 2048  # SPIFFE-ID Section 2.3
_SPIFFE_RE = re.compile(
    r"spiffe://(?P<td>[a-z0-9._-]+)/ns/(?P<ns>[A-Za-z0-9._-]+)/sa/(?P<sa>[A-Za-z0-9._-]+)"
)


@dataclass(frozen=True, slots=True)
class Identity:
    trust_domain: str
    namespace: str
    service_account: str

    @property
    def principal(self) -> str:
        """The `principals` string of AuthorizationPolicy: no scheme."""
        return f"{self.trust_domain}/ns/{self.namespace}/sa/{self.service_account}"

    @property
    def spiffe_id(self) -> str:
        return f"spiffe://{self.principal}"


def parse_spiffe_id(uri: str) -> Identity:
    if len(uri.encode()) > MAX_SPIFFE_ID_BYTES:
        raise ValueError("SPIFFE ID longer than 2048 bytes")
    match = _SPIFFE_RE.fullmatch(uri)
    if match is None:
        raise ValueError(f"not a spiffe://<td>/ns/<ns>/sa/<sa> identity: {uri!r}")
    return Identity(match["td"], match["ns"], match["sa"])


# --------------------------------------------------------------------------------------------
# AuthorizationPolicy model
# --------------------------------------------------------------------------------------------


class Action(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class Attach(StrEnum):
    SELECTOR = "selector"  # enforced where the workload's proxy runs (sidecar or ztunnel)
    TARGET_REF = "targetRef"  # enforced by a waypoint only


@dataclass(frozen=True, slots=True)
class Request:
    """What an enforcement point can know about one connection or request."""

    source: Identity | None  # peer identity from the mTLS handshake; None means plaintext
    source_ip: str
    dest_port: int
    method: str | None = None  # None means opaque TCP: no HTTP attributes exist
    path: str | None = None
    request_principal: str | None = None  # "<iss>/<sub>" from a verified JWT
    http_version: Literal["1.1", "2"] = "1.1"


@dataclass(frozen=True, slots=True)
class Source:
    principals: tuple[str, ...] = ()
    not_principals: tuple[str, ...] = ()
    namespaces: tuple[str, ...] = ()
    not_namespaces: tuple[str, ...] = ()
    ip_blocks: tuple[str, ...] = ()
    not_ip_blocks: tuple[str, ...] = ()
    request_principals: tuple[str, ...] = ()  # L7: needs RequestAuthentication
    not_request_principals: tuple[str, ...] = ()  # L7


@dataclass(frozen=True, slots=True)
class Operation:
    ports: tuple[int, ...] = ()
    not_ports: tuple[int, ...] = ()
    methods: tuple[str, ...] = ()  # L7
    not_methods: tuple[str, ...] = ()  # L7
    paths: tuple[str, ...] = ()  # L7
    not_paths: tuple[str, ...] = ()  # L7


@dataclass(frozen=True, slots=True)
class Rule:
    """`from` entries are ORed, `to` entries are ORed, the two lists are ANDed."""

    from_: tuple[Source, ...] = ()
    to: tuple[Operation, ...] = ()


@dataclass(frozen=True, slots=True)
class Policy:
    name: str
    action: Action
    rules: tuple[Rule, ...] = ()
    attach: Attach = Attach.SELECTOR


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str
    policy: str | None = None


def string_match(pattern: str, value: str) -> bool:
    """Exact, `abc*` prefix, `*abc` suffix, and `*` presence (AuthorizationPolicy reference)."""
    if pattern == "*":
        return value != ""
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    if pattern.startswith("*"):
        return value.endswith(pattern[1:])
    return value == pattern


class PathNormalization(StrEnum):
    NONE = "NONE"
    BASE = "BASE"
    MERGE_SLASHES = "MERGE_SLASHES"
    DECODE_AND_MERGE_SLASHES = "DECODE_AND_MERGE_SLASHES"


def _remove_dot_segments(path: str) -> str:
    out: list[str] = []
    segments = path.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == ".":
            if last:
                out.append("")
        elif segment == "..":
            if len(out) > 1:
                out.pop()
            if last:
                out.append("")
        else:
            out.append(segment)
    return "/".join(out)


def normalize_path(path: str, mode: PathNormalization) -> str:
    """Order from the Istio best-practices page: decode, BASE, merge slashes.

    Simplified excerpt of the documented options; Envoy's `normalize_path` does more."""
    if mode is PathNormalization.NONE:
        return path
    if mode is PathNormalization.DECODE_AND_MERGE_SLASHES:
        path = re.sub(r"%2[fF]", "/", path)
        path = re.sub(r"%5[cC]", "\\\\", path)
    path = path.replace("\\", "/")
    path = _remove_dot_segments(path)
    if mode in (PathNormalization.MERGE_SLASHES, PathNormalization.DECODE_AND_MERGE_SLASHES):
        path = re.sub(r"/{2,}", "/", path)
    return path


def _field(
    positive: Sequence[str],
    negative: Sequence[str],
    actual: str | None,
    *,
    missing_matches: bool,
    fold_case: bool = False,
) -> bool:
    if not positive and not negative:
        return True
    if actual is None:
        return missing_matches
    value = actual.upper() if fold_case else actual
    pats_pos = [p.upper() if fold_case else p for p in positive]
    pats_neg = [p.upper() if fold_case else p for p in negative]
    if pats_pos and not any(string_match(p, value) for p in pats_pos):
        return False
    return not any(string_match(n, value) for n in pats_neg)


def _in_blocks(blocks: Sequence[str], ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    return any(address in ipaddress.ip_network(b, strict=False) for b in blocks)


@dataclass(frozen=True, slots=True)
class EnvoyEnforcer:
    """An Envoy RBAC enforcement point: a sidecar's inbound listener or a waypoint.

    `case_sensitive_methods=True` reproduces the pre-fix behaviour of GHSA-w44g-8m76-8gc5
    (ISTIO-SECURITY-2026-007); `False` is the fixed behaviour."""

    path_normalization: PathNormalization = PathNormalization.BASE
    case_sensitive_methods: bool = False

    def _source_ok(self, src: Source, req: Request, missing: bool) -> bool:
        principal = req.source.principal if req.source else None
        namespace = req.source.namespace if req.source else None
        if not _field(src.principals, src.not_principals, principal or "", missing_matches=missing):
            return False
        if not _field(src.namespaces, src.not_namespaces, namespace or "", missing_matches=missing):
            return False
        if src.ip_blocks and not _in_blocks(src.ip_blocks, req.source_ip):
            return False
        if src.not_ip_blocks and _in_blocks(src.not_ip_blocks, req.source_ip):
            return False
        return _field(
            src.request_principals,
            src.not_request_principals,
            req.request_principal or "",
            missing_matches=missing,
        )

    def _operation_ok(self, op: Operation, req: Request, missing: bool) -> bool:
        if op.ports and req.dest_port not in op.ports:
            return False
        if req.dest_port in op.not_ports:
            return False
        path = None if req.path is None else normalize_path(req.path, self.path_normalization)
        return _field(
            op.methods,
            op.not_methods,
            req.method,
            missing_matches=missing,
            fold_case=not self.case_sensitive_methods,
        ) and _field(op.paths, op.not_paths, path, missing_matches=missing)

    def _rule_ok(self, rule: Rule, req: Request, action: Action) -> bool:
        missing = action is Action.DENY  # "for a DENY rule, missing attributes are matches"
        from_ok = not rule.from_ or any(self._source_ok(s, req, missing) for s in rule.from_)
        to_ok = not rule.to or any(self._operation_ok(o, req, missing) for o in rule.to)
        return from_ok and to_ok

    def evaluate(self, policies: Sequence[Policy], req: Request) -> Decision:
        if (
            req.method is not None
            and req.http_version == "1.1"
            and req.method != req.method.upper()
        ):
            return Decision(False, "HTTP/1.1 codec rejects a non-uppercase method")
        for pol in (p for p in policies if p.action is Action.DENY):
            if any(self._rule_ok(r, req, pol.action) for r in pol.rules):
                return Decision(False, "DENY policy matched", pol.name)
        allow = [p for p in policies if p.action is Action.ALLOW]
        if not allow:
            return Decision(True, "no ALLOW policy applies")
        for pol in allow:
            if any(self._rule_ok(r, req, pol.action) for r in pol.rules):
                return Decision(True, "ALLOW policy matched", pol.name)
        return Decision(False, "no ALLOW policy matched")


def principal_pattern_to_regex(pattern: str, *, escape: bool) -> re.Pattern[str]:
    """Illustrates the bug class of CVE-2026-39350 (unescaped dots in `serviceAccounts`).

    Istio 1.32.0 `pilot/pkg/security/authz/model/generator.go`, `serviceAccountRegex`, builds
    `spiffe://.+/ns/%s/(.+/|)sa/%s(/.+)?` and wraps both values in `regexp.QuoteMeta`;
    this helper models only the escape step, not that full pattern."""
    body = re.escape(pattern) if escape else pattern
    return re.compile(body)


# --------------------------------------------------------------------------------------------
# ztunnel: lowering (istiod) and L4 evaluation
# (istio 1.32.0 pilot/pkg/serviceregistry/ambient/authorization.go, convertAuthorizationPolicy,
#  handleRule; ztunnel 1.32.0 src/rbac.rs, Authorization.matches; src/state.rs, assert_rbac)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZtMatch:
    namespaces: tuple[str, ...] = ()
    not_namespaces: tuple[str, ...] = ()
    principals: tuple[str, ...] = ()
    not_principals: tuple[str, ...] = ()
    source_ips: tuple[str, ...] = ()
    not_source_ips: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()
    not_ports: tuple[int, ...] = ()

    def is_empty(self) -> bool:
        return self == ZtMatch()


ZtClause = tuple[ZtMatch, ...]  # matches are ORed
ZtGroup = tuple[ZtClause, ...]  # clauses are ANDed; groups are ORed


@dataclass(frozen=True, slots=True)
class ZtAuthorization:
    name: str
    action: Action
    groups: tuple[ZtGroup, ...]
    unsupported: tuple[str, ...] = ()


_L7_OPERATION = ("methods", "not_methods", "paths", "not_paths")
_L7_SOURCE = ("request_principals", "not_request_principals")


def _lower_rule(action: Action, rule: Rule) -> tuple[ZtGroup | None, set[str]]:
    found: set[str] = set()
    clauses: list[ZtClause] = []
    if rule.to:
        matches: list[ZtMatch] = []
        for op in rule.to:
            found |= {f for f in _L7_OPERATION if getattr(op, f)}
            matches.append(ZtMatch(ports=op.ports, not_ports=op.not_ports))
        clauses.append(tuple(matches))
    if rule.from_:
        matches = []
        for src in rule.from_:
            found |= {f for f in _L7_SOURCE if getattr(src, f)}
            matches.append(
                ZtMatch(
                    namespaces=src.namespaces,
                    not_namespaces=src.not_namespaces,
                    principals=src.principals,
                    not_principals=src.not_principals,
                    source_ips=src.ip_blocks,
                    not_source_ips=src.not_ip_blocks,
                )
            )
        clauses.append(tuple(matches))
    if action is Action.ALLOW and found:
        return None, found  # handleRule: "L7 policies never match for ALLOW"
    return tuple(clauses), found


def lower_policy(policy: Policy) -> ZtAuthorization | None:
    """None for a `targetRef` policy: ztunnel never receives it."""
    if policy.attach is Attach.TARGET_REF:
        return None
    groups: list[ZtGroup] = []
    unsupported: set[str] = set()
    for rule in policy.rules:
        group, found = _lower_rule(policy.action, rule)
        unsupported |= found
        if group is not None:
            groups.append(group)
    return ZtAuthorization(policy.name, policy.action, tuple(groups), tuple(sorted(unsupported)))


def _internal[T](
    positive: Sequence[T], negative: Sequence[T], predicate: Callable[[T], bool]
) -> bool:
    pos = True if not positive else any(predicate(p) for p in positive)
    neg = True if not negative else not any(predicate(n) for n in negative)
    return pos and neg


def _zt_one(m: ZtMatch, req: Request) -> bool:
    spiffe = req.source.spiffe_id if req.source else ""
    namespace = req.source.namespace if req.source else ""
    principal = spiffe.removeprefix("spiffe://") if spiffe.startswith("spiffe://") else None
    return (
        _internal(m.source_ips, m.not_source_ips, lambda b: _in_blocks([b], req.source_ip))
        and _internal(m.ports, m.not_ports, lambda p: p == req.dest_port)
        and _internal(
            m.principals,
            m.not_principals,
            lambda p: principal is not None and string_match(p, principal),
        )
        and _internal(m.namespaces, m.not_namespaces, lambda n: string_match(n, namespace))
    )


def zt_matches(auth: ZtAuthorization, req: Request) -> bool:
    """ztunnel Authorization.matches: empty matches are skipped, an empty clause list matches."""
    if not auth.groups:
        return False
    for group in auth.groups:
        group_ok = True
        for clause in group:
            clause_ok = any(_zt_one(m, req) for m in clause if not m.is_empty())
            if not clause:
                clause_ok = True
            group_ok = group_ok and clause_ok
            if not group_ok:
                break
        if group_ok:
            return True
    return False


def evaluate_ztunnel(policies: Sequence[Policy], req: Request) -> Decision:
    lowered = [z for p in policies if (z := lower_policy(p)) is not None]
    for auth in (z for z in lowered if z.action is Action.DENY):
        if zt_matches(auth, req):
            return Decision(False, "DENY policy matched at ztunnel", auth.name)
    allow = [z for z in lowered if z.action is Action.ALLOW]
    if not allow:
        return Decision(True, "no ALLOW policy applies")
    for auth in allow:
        if zt_matches(auth, req):
            return Decision(True, "ALLOW policy matched at ztunnel", auth.name)
    return Decision(False, "no ALLOW policy matched at ztunnel")


# --------------------------------------------------------------------------------------------
# PeerAuthentication in ambient (convertPeerAuthentication, simplified excerpt)
# --------------------------------------------------------------------------------------------


class MtlsMode(StrEnum):
    UNSET = "UNSET"
    DISABLE = "DISABLE"
    PERMISSIVE = "PERMISSIVE"
    STRICT = "STRICT"


def effective_mtls_mode(
    mesh: MtlsMode,
    namespace: MtlsMode,
    workload: MtlsMode,
    port_modes: dict[int, MtlsMode],
    port: int,
) -> MtlsMode:
    """Narrowest set mode wins: port, workload, namespace, mesh; nothing set is PERMISSIVE."""
    for mode in (port_modes.get(port, MtlsMode.UNSET), workload, namespace, mesh):
        if mode is not MtlsMode.UNSET:
            return mode
    return MtlsMode.PERMISSIVE


def lower_peer_authentication(
    workload_mode: MtlsMode, port_modes: dict[int, MtlsMode]
) -> ZtAuthorization | None:
    """STRICT lowers to a DENY on "no principal"; port overrides become extra clauses or groups."""
    no_principal = ZtMatch(not_principals=("*",))
    groups: list[ZtGroup] = []
    if workload_mode is MtlsMode.STRICT:
        clauses: list[ZtClause] = [(no_principal,)]
        clauses += [
            (ZtMatch(not_ports=(p,)),)
            for p, m in sorted(port_modes.items())
            if m in (MtlsMode.PERMISSIVE, MtlsMode.DISABLE)
        ]
        groups.append(tuple(clauses))
    else:
        for port, mode in sorted(port_modes.items()):
            if mode is MtlsMode.STRICT:
                groups.append(((ZtMatch(not_principals=("*",), ports=(port,)),),))
    if not groups:
        return None
    return ZtAuthorization("peer-authentication", Action.DENY, tuple(groups))


# --------------------------------------------------------------------------------------------
# Traffic path: sidecar versus ambient with an optional waypoint
# --------------------------------------------------------------------------------------------


class DataPlane(StrEnum):
    SIDECAR = "sidecar"
    AMBIENT = "ambient"


@dataclass(frozen=True, slots=True)
class Workload:
    name: str
    identity: Identity
    node: str
    ip: str
    waypoint: Identity | None = None  # identity of the waypoint that fronts it, if enrolled


@dataclass(frozen=True, slots=True)
class Hop:
    proxy: str
    decision: Decision


@dataclass(frozen=True, slots=True)
class Delivery:
    allowed: bool
    hops: tuple[Hop, ...]
    via_waypoint: bool


def deliver(
    mode: DataPlane,
    req: Request,
    dest: Workload,
    policies: Sequence[Policy],
    *,
    dial: Literal["service", "workload"] = "service",
    waypoint_for: Literal["service", "workload", "all", "none"] = "service",
    envoy: EnvoyEnforcer = EnvoyEnforcer(),  # noqa: B008 - frozen, shared on purpose
) -> Delivery:
    selector = [p for p in policies if p.attach is Attach.SELECTOR]
    if mode is DataPlane.SIDECAR:
        decision = envoy.evaluate(selector, req)
        return Delivery(decision.allowed, (Hop("dest-sidecar", decision),), False)
    hops: list[Hop] = []
    uses_waypoint = dest.waypoint is not None and waypoint_for in (dial, "all")
    inbound = req
    if uses_waypoint and dest.waypoint is not None:
        attached = [p for p in policies if p.attach is Attach.TARGET_REF]
        decision = envoy.evaluate(attached, req)  # the waypoint still sees the client identity
        hops.append(Hop("waypoint", decision))
        if not decision.allowed:
            return Delivery(False, tuple(hops), True)
        inbound = replace(req, source=dest.waypoint)  # destination ztunnel sees the waypoint
    decision = evaluate_ztunnel(selector, inbound)
    hops.append(Hop("dest-ztunnel", decision))
    return Delivery(decision.allowed, tuple(hops), uses_waypoint)


# --------------------------------------------------------------------------------------------
# X.509 SVIDs, peer verification, and rotation
# --------------------------------------------------------------------------------------------


class PeerRejected(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Authority:
    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey


def make_authority(name: str, *, now: datetime, years: int = 10) -> Authority:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365 * years))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return Authority(cert, key)


def issue_svid(
    authority: Authority,
    uris: Sequence[str],
    *,
    now: datetime,
    ttl: timedelta,
    dns_names: Sequence[str] = (),
) -> x509.Certificate:
    """X509-SVID 3.1 and 4.1: no subject, critical URI SAN, cA false; key usage (4.3) unset."""
    names: list[x509.GeneralName] = [x509.UniformResourceIdentifier(u) for u in uris]
    names += [x509.DNSName(d) for d in dns_names]
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(authority.certificate.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + ttl)
        .add_extension(x509.SubjectAlternativeName(names), critical=True)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(authority.key, hashes.SHA256())
    )


def svid_identity(cert: x509.Certificate) -> Identity:
    """Exactly one URI SAN, a non-CA leaf, and the Istio identity layout (X509-SVID Section 5.2)."""
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    if basic.ca:
        raise PeerRejected("a CA certificate is not a leaf SVID")
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    if len(uris) != 1:
        raise PeerRejected(f"expected exactly one URI SAN, found {len(uris)}")
    try:
        return parse_spiffe_id(uris[0])
    except ValueError as exc:
        raise PeerRejected(str(exc)) from exc


def verify_peer(
    cert: x509.Certificate,
    authority: x509.Certificate,
    expected: Sequence[Identity],
    *,
    now: datetime,
) -> Identity:
    """What ztunnel's IdentityVerifier does: issuer, validity, then URI SAN equality.

    The DNS name of the connection is never consulted
    (ztunnel 1.32.0 src/tls/workload.rs, IdentityVerifier::verify_full_san)."""
    try:
        cert.verify_directly_issued_by(authority)
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise PeerRejected(f"not issued by the trust anchor: {exc}") from exc
    if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
        raise PeerRejected("certificate outside its validity window")
    identity = svid_identity(cert)
    if identity not in expected:
        raise PeerRejected(f"identity {identity.spiffe_id} is not an expected peer")
    return identity


def refresh_deadline(cert: x509.Certificate) -> datetime:
    """ztunnel WorkloadCertificate::refresh_at: not_before plus half the validity."""
    return cert.not_valid_before_utc + (cert.not_valid_after_utc - cert.not_valid_before_utc) / 2


RETRY_INITIAL_S = 1.0
RETRY_MULTIPLIER = 2.0
RETRY_MAX_S = 150.0  # CERT_REFRESH_FAILURE_RETRY_DELAY_MAX_INTERVAL; +-20% jitter omitted here


@dataclass(frozen=True, slots=True)
class CaOutage:
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class RotationResult:
    issued_at: tuple[float, ...]
    failed_attempts: int
    gaps: tuple[tuple[float, float], ...]  # intervals with no valid certificate


def simulate_rotation(
    *,
    ttl_s: float,
    refresh_fraction: float,
    outages: Sequence[CaOutage],
    horizon_s: float,
) -> RotationResult:
    """One identity: refresh at `refresh_fraction` of the lifetime, retry with capped backoff,
    keep serving the old certificate until it expires (identity/manager.rs, the refresh loop)."""

    def ca_up(t: float) -> bool:
        return not any(o.start <= t < o.end for o in outages)

    valid_until = ttl_s
    next_t = ttl_s * refresh_fraction
    delay = RETRY_INITIAL_S
    issued = [0.0]
    failed = 0
    gaps: list[tuple[float, float]] = []
    gap_start: float | None = None
    while next_t < horizon_s:
        t = next_t
        if t >= valid_until and gap_start is None:
            gap_start = valid_until
        if ca_up(t):
            if gap_start is not None:
                gaps.append((gap_start, t))
                gap_start = None
            valid_until = t + ttl_s
            issued.append(t)
            next_t = t + ttl_s * refresh_fraction
            delay = RETRY_INITIAL_S
        else:
            failed += 1
            next_t = t + delay
            delay = min(delay * RETRY_MULTIPLIER, RETRY_MAX_S)
            if t < valid_until:
                next_t = min(next_t, valid_until)
    if gap_start is not None:
        gaps.append((gap_start, horizon_s))
    return RotationResult(tuple(issued), failed, tuple(gaps))


def rotate_in(
    *,
    lifetime_s: float,
    age_s: float,
    grace_ratio: float,
    jitter: float,
    rng: random.Random,
) -> float:
    """istio 1.32.0 security/pkg/nodeagent/cache/secretcache.go, rotateTime: seconds until the
    sidecar agent rotates, with the grace ratio jittered up or down and clamped to [0, 1]."""
    offset = rng.random() * jitter * (1 if rng.randrange(2) else -1)
    ratio = min(max(grace_ratio + offset, 0.0), 1.0)
    return max(lifetime_s - ratio * lifetime_s - age_s, 0.0)


# --------------------------------------------------------------------------------------------
# Node-scoped issuance (istio 1.32.0 security/pkg/server/ca/node_auth.go)
# --------------------------------------------------------------------------------------------


class ImpersonationDenied(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Pod:
    name: str
    namespace: str
    uid: str
    service_account: str
    node: str


@dataclass(frozen=True, slots=True)
class NodeAuthorizer:
    trusted_node_accounts: frozenset[tuple[str, str]]  # (namespace, service account) of ztunnels
    pods: tuple[Pod, ...]

    def authorize(self, caller: Pod, caller_uid: str, requested: Identity) -> None:
        """Raise unless `caller` may obtain a certificate for `requested`."""
        if (caller.namespace, caller.service_account) not in self.trusted_node_accounts:
            raise ImpersonationDenied("caller is not a trusted node account")
        current = next(
            (p for p in self.pods if (p.name, p.namespace) == (caller.name, caller.namespace)),
            None,
        )
        if current is None:
            raise ImpersonationDenied("caller pod not found")
        if current.uid != caller_uid:
            raise ImpersonationDenied("caller pod UID is stale")
        runs_here = any(
            (p.namespace, p.service_account, p.node)
            == (requested.namespace, requested.service_account, current.node)
            for p in self.pods
        )
        if not runs_here:
            raise ImpersonationDenied("requested identity does not run on the caller's node")


# --------------------------------------------------------------------------------------------
# HBONE: HTTP/2 CONNECT streams, one connection per (source, destination) identity pair
# --------------------------------------------------------------------------------------------

HBONE_PORT = 15008
POOL_MAX_STREAMS_PER_CONNECTION = 100  # ztunnel 1.32.0 src/config.rs, same default as h2 peers


def serve_hbone(headers: Sequence[tuple[str, str]], decision: Decision) -> int:
    """Status choices of ztunnel inbound.rs (build_inbound_request, serve_connect)."""
    if dict(headers).get(":method") != "CONNECT":
        return 400
    if not decision.allowed:
        return 401
    return 200


def h2_pair(max_streams: int) -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client = h2.connection.H2Connection(
        config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
    )
    server = h2.connection.H2Connection(
        config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
    )
    server.local_settings = h2.settings.Settings(
        client=False, initial_values={h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: max_streams}
    )
    client.initiate_connection()
    server.initiate_connection()
    for _ in range(3):
        server.receive_data(client.data_to_send())
        client.receive_data(server.data_to_send())
    return client, server


@dataclass(slots=True)
class HboneConnection:
    key: tuple[Identity, Identity]
    client: h2.connection.H2Connection
    server: h2.connection.H2Connection
    streams: int = 0
    next_stream_id: int = 1


class HbonePool:
    def __init__(self, max_streams: int = POOL_MAX_STREAMS_PER_CONNECTION) -> None:
        self.max_streams = max_streams
        self.connections: list[HboneConnection] = []

    def open_tunnel(
        self, src: Identity, dst: Identity, authority: str
    ) -> tuple[HboneConnection, int, list[tuple[str, str]]]:
        """Open a CONNECT stream; returns the connection, the stream id, and the request headers
        the server side decoded."""
        conn = next(
            (c for c in self.connections if c.key == (src, dst) and c.streams < self.max_streams),
            None,
        )
        if conn is None:
            client, server = h2_pair(self.max_streams)
            conn = HboneConnection((src, dst), client, server)
            self.connections.append(conn)
        stream_id = conn.next_stream_id
        conn.next_stream_id += 2
        conn.client.send_headers(stream_id, [(":method", "CONNECT"), (":authority", authority)])
        seen: list[tuple[str, str]] = []
        for event in conn.server.receive_data(conn.client.data_to_send()):
            if isinstance(event, h2.events.RequestReceived):
                seen = [(str(k), str(v)) for k, v in event.headers]
        conn.streams += 1
        return conn, stream_id, seen


def tunnel_bytes(conn: HboneConnection, stream_id: int, payload: bytes) -> bytes:
    """Send `payload` as DATA on a CONNECT stream and return what the server side received."""
    conn.server.send_headers(stream_id, [(":status", "200")])
    conn.client.receive_data(conn.server.data_to_send())
    conn.client.send_data(stream_id, payload)
    received = b""
    for event in conn.server.receive_data(conn.client.data_to_send()):
        if isinstance(event, h2.events.DataReceived):
            received += event.data
    return received


# --------------------------------------------------------------------------------------------
# Footprint arithmetic from the Istio 1.24 performance page (1000 rps, 1 KB payload, 2 workers)
# --------------------------------------------------------------------------------------------

PROXY_MEMORY_MB = {"sidecar": 60.0, "waypoint": 60.0, "ztunnel": 12.0}
PROXY_VCPU = {"sidecar": 0.20, "waypoint": 0.25, "ztunnel": 0.06}
LATENCY_BAND_MS = {  # "Sidecar or ambient?" table, as of 2026-11
    "sidecar": (0.63, 0.88),
    "ambient": (0.16, 0.20),
    "ambient+waypoint": (0.40, 0.50),
}


@dataclass(frozen=True, slots=True)
class Fleet:
    nodes: int
    pods_per_node: int
    waypoints: int = 0


@dataclass(frozen=True, slots=True)
class Footprint:
    proxies: int
    memory_mb: float
    vcpu: float


def footprint(mode: DataPlane, fleet: Fleet) -> Footprint:
    """Linear scaling of the documented per-proxy figure: a model, not a measurement."""
    if mode is DataPlane.SIDECAR:
        n = fleet.nodes * fleet.pods_per_node
        return Footprint(n, n * PROXY_MEMORY_MB["sidecar"], n * PROXY_VCPU["sidecar"])
    return Footprint(
        fleet.nodes + fleet.waypoints,
        fleet.nodes * PROXY_MEMORY_MB["ztunnel"] + fleet.waypoints * PROXY_MEMORY_MB["waypoint"],
        fleet.nodes * PROXY_VCPU["ztunnel"] + fleet.waypoints * PROXY_VCPU["waypoint"],
    )


def max_waypoints_before_ambient_costs_more(fleet: Fleet) -> int:
    """Largest waypoint count whose ambient memory still does not exceed the sidecar fleet's."""
    sidecar = footprint(DataPlane.SIDECAR, fleet).memory_mb
    spare = sidecar - fleet.nodes * PROXY_MEMORY_MB["ztunnel"]
    return int(spare // PROXY_MEMORY_MB["waypoint"])


def proxy_hops(mode: DataPlane, *, waypoint: bool) -> int:
    """Proxies a request crosses: two sidecars, two ztunnels, or two ztunnels plus a waypoint."""
    if mode is DataPlane.SIDECAR:
        return 2
    return 3 if waypoint else 2
