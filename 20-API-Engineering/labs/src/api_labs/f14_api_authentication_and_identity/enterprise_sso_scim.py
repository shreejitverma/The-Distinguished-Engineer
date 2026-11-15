"""Multi-tenant OIDC issuer validation and SCIM 2.0 deprovisioning for a B2B API.

Lab for the note Enterprise-SSO-Integration-Multi-Tenant-OIDC-and-SCIM-Provisioning.
Everything is in-process: keys, tokens, the directory, and the Starlette app.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

import jwt
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
    from starlette.requests import Request

ENTRA_V2_ISSUER_TEMPLATE: Final = "https://login.microsoftonline.com/{tenantid}/v2.0"
PATCH_OP_SCHEMA: Final = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA: Final = "urn:ietf:params:scim:api:messages:2.0:Error"
LIST_SCHEMA: Final = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
USER_SCHEMA: Final = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA: Final = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_MEDIA_TYPE: Final = "application/scim+json"

_GUID: Final = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_MEMBER_FILTER: Final = re.compile(r'\Amembers\[value eq "([^"]+)"\]\Z', re.IGNORECASE)
_EQ_FILTER: Final = re.compile(r'\A(userName|externalId) eq "([^"]*)"\Z', re.IGNORECASE)
_USER_URN_PREFIX: Final = USER_SCHEMA.lower() + ":"


# --------------------------------------------------------------------------- OIDC side


class TokenRejectedError(Exception):
    """A token failed validation; `reason` is safe to log, never echo it to the caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SigningKey:
    """One JWK. `issuer` is exact, or an Entra template containing `{tenantid}`."""

    kid: str
    public_key: EllipticCurvePublicKey
    issuer: str


@dataclass(frozen=True, slots=True)
class TenantConnection:
    """One customer's IdP connection, configured at onboarding, never derived from a token."""

    tenant: str
    issuer: str
    client_id: str
    api_audience: str
    subject_claim: str = "sub"


@dataclass(frozen=True, slots=True)
class Principal:
    tenant: str
    issuer: str
    subject: str


class IssuerRegistry:
    """Maps each onboarded issuer to exactly one tenant and validates tokens against it."""

    def __init__(
        self,
        keys: Iterable[SigningKey],
        connections: Iterable[TenantConnection],
        *,
        algorithms: tuple[str, ...] = ("ES256",),
        leeway_seconds: int = 60,
    ) -> None:
        self._keys = {key.kid: key for key in keys}
        self._by_tenant: dict[str, TenantConnection] = {}
        self._by_issuer: dict[str, TenantConnection] = {}
        for conn in connections:
            if conn.issuer in self._by_issuer or conn.tenant in self._by_tenant:
                raise ValueError(f"issuer or tenant registered twice: {conn.issuer!r}")
            if "{" in conn.issuer:
                raise ValueError("a tenant connection needs a concrete issuer, not a template")
            self._by_issuer[conn.issuer] = conn
            self._by_tenant[conn.tenant] = conn
        self._algorithms = list(algorithms)
        self._leeway = leeway_seconds

    def connection(self, tenant: str) -> TenantConnection:
        try:
            return self._by_tenant[tenant]
        except KeyError:
            raise TokenRejectedError("unknown tenant") from None

    def tenant_for_issuer(self, token: str) -> str:
        """Route a token by its unverified `iss`; `validate` re-checks everything after."""
        try:
            iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError:
            raise TokenRejectedError("malformed token") from None
        conn = self._by_issuer.get(iss) if isinstance(iss, str) else None
        if conn is None:
            raise TokenRejectedError("issuer not onboarded")
        return conn.tenant

    def validate(self, token: str, *, tenant: str, audience: str) -> Principal:
        conn = self.connection(tenant)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenRejectedError("malformed token") from None
        key = self._keys.get(str(header.get("kid")))
        if key is None:
            raise TokenRejectedError("unknown kid")
        try:
            claims = jwt.decode(
                token,
                key.public_key,
                algorithms=self._algorithms,
                audience=audience,
                issuer=conn.issuer,  # exact string match, OIDC Core 3.1.3.7 step 2
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise TokenRejectedError(type(exc).__name__) from None
        self._check_key_issuer(key, claims)
        subject = claims.get(conn.subject_claim)
        if not isinstance(subject, str) or not subject:
            raise TokenRejectedError(f"missing {conn.subject_claim}")
        return Principal(tenant=conn.tenant, issuer=conn.issuer, subject=subject)

    @staticmethod
    def _check_key_issuer(key: SigningKey, claims: dict[str, Any]) -> None:
        """Bind the signing key to the token issuer (Entra keys carry an `issuer` property).

        The tenant allowlist is the exact `conn.issuer` match above; this check stops a key
        scoped to one issuer, or to the `{tenantid}` template, from vouching for another.
        """
        if "{tenantid}" not in key.issuer:
            if key.issuer != claims["iss"]:
                raise TokenRejectedError("key issuer mismatch")
            return
        tid = claims.get("tid")
        if not isinstance(tid, str) or _GUID.fullmatch(tid) is None:
            raise TokenRejectedError("tid is not a GUID")
        if key.issuer.replace("{tenantid}", tid) != claims["iss"]:
            raise TokenRejectedError("iss does not embed tid")


# --------------------------------------------------------------------------- SCIM side


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(self.status)}
        if self.scim_type is not None:
            out["scimType"] = self.scim_type
        out["detail"] = self.detail
        return out


@dataclass(slots=True)
class ScimUser:
    id: str
    tenant: str
    user_name: str
    external_id: str | None
    display_name: str | None
    active: bool
    version: int = 1

    @property
    def etag(self) -> str:
        return f'W/"{self.version}"'

    def to_scim(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schemas": [USER_SCHEMA],
            "id": self.id,
            "userName": self.user_name,
            "active": self.active,
            "meta": {"resourceType": "User", "version": self.etag},
        }
        if self.external_id is not None:
            out["externalId"] = self.external_id
        if self.display_name is not None:
            out["displayName"] = self.display_name
        return out


@dataclass(slots=True)
class ScimGroup:
    id: str
    tenant: str
    display_name: str
    members: set[str] = field(default_factory=set[str])


@dataclass(frozen=True, slots=True)
class Session:
    tenant: str
    user_id: str


@dataclass(frozen=True, slots=True)
class PatchOp:
    op: str
    path: str | None
    value: Any


def _obj(value: Any) -> dict[str, Any]:
    """Narrow decoded JSON to an object (empty when it is not one)."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _arr(value: Any) -> list[object] | None:
    return cast("list[object]", value) if isinstance(value, list) else None


def _opt_str(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is not None and not isinstance(value, str):
        raise ScimError(400, f"{name} must be a string", "invalidValue")
    return value


def _coerce_bool(value: Any) -> bool:
    """RFC 7643 2.3.2 says JSON true/false; legacy Entra sends the string "False"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise ScimError(400, f"active must be a boolean, got {value!r}", "invalidValue")


def _parse_patch(body: Any) -> list[PatchOp]:
    schemas = _obj(body).get("schemas")
    if not isinstance(schemas, list) or PATCH_OP_SCHEMA not in schemas:
        raise ScimError(400, "body must declare the PatchOp schema", "invalidSyntax")
    raw_ops = _arr(_obj(body).get("Operations"))
    if not raw_ops:
        raise ScimError(400, "Operations must be a non-empty array", "invalidSyntax")
    ops: list[PatchOp] = []
    for item in raw_ops:
        raw = _obj(item)
        name = raw.get("op")
        if not isinstance(name, str):
            raise ScimError(400, "each operation needs an op", "invalidSyntax")
        op = name.lower()  # Entra emits "Replace", RFC examples use "replace"
        if op not in {"add", "remove", "replace"}:
            raise ScimError(400, f"unsupported op {name!r}", "invalidSyntax")
        path = raw.get("path")
        if path is not None and not isinstance(path, str):
            raise ScimError(400, "path must be a string", "invalidPath")
        ops.append(PatchOp(op=op, path=path, value=raw.get("value")))
    return ops


def _set_user_attr(user: ScimUser, op: str, name: str, value: Any) -> None:
    attr = name.lower().removeprefix(_USER_URN_PREFIX)  # RFC 7643 2.1: names are case insensitive
    if op == "remove":
        if attr in {"username", "active"}:
            raise ScimError(400, f"{name} cannot be removed", "mutability")
        value = None
    if attr == "active":
        user.active = _coerce_bool(value)
    elif attr == "username":
        if not isinstance(value, str) or not value:
            raise ScimError(400, "userName must be a non-empty string", "invalidValue")
        user.user_name = value
    elif attr in {"displayname", "externalid"}:
        if value is not None and not isinstance(value, str):
            raise ScimError(400, f"{name} must be a string", "invalidValue")
        if attr == "displayname":
            user.display_name = value
        else:
            user.external_id = value
    else:
        raise ScimError(400, f"unsupported attribute path {name!r}", "invalidPath")


class Directory:
    """Tenant-partitioned SCIM store plus the session table it must revoke from."""

    def __init__(self) -> None:
        self.users: dict[tuple[str, str], ScimUser] = {}
        self.groups: dict[tuple[str, str], ScimGroup] = {}
        self.sessions: dict[str, Session] = {}
        self.revocations: list[tuple[str, str, str, int]] = []

    # users ---------------------------------------------------------------

    def create_user(self, tenant: str, body: Any) -> ScimUser:
        fields = _obj(body)
        user_name = _opt_str(fields, "userName")
        if not user_name:
            raise ScimError(400, "userName is required", "invalidValue")
        wanted = user_name.lower()  # RFC 7643 4.1.1: userName is case insensitive
        if any(u.tenant == tenant and u.user_name.lower() == wanted for u in self.users.values()):
            raise ScimError(409, "userName already exists in this tenant", "uniqueness")
        user = ScimUser(
            id=str(uuid.uuid4()),
            tenant=tenant,
            user_name=user_name,
            external_id=_opt_str(fields, "externalId"),
            display_name=_opt_str(fields, "displayName"),
            active=_coerce_bool(fields.get("active", True)),
        )
        self.users[(tenant, user.id)] = user
        return user

    def get_user(self, tenant: str, user_id: str) -> ScimUser:
        user = self.users.get((tenant, user_id))
        if user is None:  # another tenant's id is indistinguishable from a missing one
            raise ScimError(404, f"Resource {user_id} not found")
        return user

    def user_for_subject(self, tenant: str, subject: str) -> ScimUser | None:
        """The SSO join: the IdP's immutable subject, stored by SCIM as externalId."""
        matches = [
            u for u in self.users.values() if u.tenant == tenant and u.external_id == subject
        ]
        return matches[0] if len(matches) == 1 else None  # ambiguous joins fail closed

    def find_users(self, tenant: str, scim_filter: str | None) -> list[ScimUser]:
        users = [u for u in self.users.values() if u.tenant == tenant]
        if scim_filter is None:
            return users
        match = _EQ_FILTER.fullmatch(scim_filter.strip())
        if match is None:
            raise ScimError(
                400, "only userName eq and externalId eq are supported", "invalidFilter"
            )
        attr, wanted = match.group(1).lower(), match.group(2)
        if attr == "username":
            return [u for u in users if u.user_name.lower() == wanted.lower()]
        return [u for u in users if u.external_id == wanted]  # externalId is caseExact

    def patch_user(
        self, tenant: str, user_id: str, body: Any, if_match: str | None = None
    ) -> ScimUser:
        current = self.get_user(tenant, user_id)
        if if_match is not None and if_match != current.etag:
            raise ScimError(412, "resource changed since the supplied ETag")
        draft = dataclasses.replace(current)  # RFC 7644 3.5.2: the whole PATCH is atomic
        for op in _parse_patch(body):
            if op.path is None:
                if op.op == "remove":
                    raise ScimError(400, "remove requires a path", "noTarget")
                if not _obj(op.value):  # RFC 7644 3.5.2.3: one or more attributes
                    raise ScimError(400, "pathless op needs an object value", "invalidValue")
                for name, value in _obj(op.value).items():
                    _set_user_attr(draft, op.op, name, value)
            else:
                _set_user_attr(draft, op.op, op.path, op.value)
        draft.version = current.version + 1
        self.users[(tenant, user_id)] = draft
        if current.active and not draft.active:
            self._revoke_sessions(tenant, user_id, "scim active=false")
        return draft

    def delete_user(self, tenant: str, user_id: str) -> None:
        self.get_user(tenant, user_id)
        del self.users[(tenant, user_id)]
        for group in self.groups.values():
            if group.tenant == tenant:
                group.members.discard(user_id)
        self._revoke_sessions(tenant, user_id, "scim delete")

    # groups --------------------------------------------------------------

    def create_group(self, tenant: str, body: Any) -> ScimGroup:
        display_name = _opt_str(_obj(body), "displayName")
        if not display_name:
            raise ScimError(400, "displayName is required", "invalidValue")
        group = ScimGroup(id=str(uuid.uuid4()), tenant=tenant, display_name=display_name)
        self.groups[(tenant, group.id)] = group
        return group

    def patch_group(self, tenant: str, group_id: str, body: Any) -> None:
        group = self.groups.get((tenant, group_id))
        if group is None:
            raise ScimError(404, f"Resource {group_id} not found")
        members = set(group.members)
        for op in _parse_patch(body):
            path = (op.path or "").strip()
            filtered = _MEMBER_FILTER.fullmatch(path)
            if op.op == "remove" and filtered is not None:
                members.discard(filtered.group(1))
            elif path.lower() != "members":
                raise ScimError(400, f"unsupported group path {op.path!r}", "invalidPath")
            elif op.op == "remove" and op.value is None:
                members.clear()  # RFC 7644 3.5.2.2: no filter removes every value
            elif op.op == "remove":
                members -= self._member_ids(tenant, op.value, must_exist=False)
            elif op.op == "add":
                members |= self._member_ids(tenant, op.value, must_exist=True)
            else:
                members = self._member_ids(tenant, op.value, must_exist=True)
        group.members = members

    def _member_ids(self, tenant: str, value: Any, *, must_exist: bool) -> set[str]:
        items = _arr(value)
        if items is None:
            raise ScimError(400, "members value must be an array", "invalidValue")
        ids: set[str] = set()
        for item in items:
            member = _obj(item).get("value")
            if not isinstance(member, str):
                raise ScimError(400, "each member needs a value", "invalidValue")
            if must_exist and (tenant, member) not in self.users:
                raise ScimError(
                    400, f"member {member} is not a user of this tenant", "invalidValue"
                )
            ids.add(member)
        return ids

    def is_member(self, tenant: str, user_id: str, group_name: str) -> bool:
        return any(
            g.tenant == tenant and g.display_name == group_name and user_id in g.members
            for g in self.groups.values()
        )

    # sessions ------------------------------------------------------------

    def open_session(self, tenant: str, user_id: str) -> str:
        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = Session(tenant=tenant, user_id=user_id)
        return session_id

    def _revoke_sessions(self, tenant: str, user_id: str, reason: str) -> None:
        doomed = [
            sid for sid, s in self.sessions.items() if s.tenant == tenant and s.user_id == user_id
        ]
        for sid in doomed:
            del self.sessions[sid]
        self.revocations.append((tenant, user_id, reason, len(doomed)))


# --------------------------------------------------------------------------- HTTP surface


def _scim(body: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> Response:
    return JSONResponse(body, status_code=status, headers=headers, media_type=SCIM_MEDIA_TYPE)


def _scim_error(err: ScimError) -> Response:
    return _scim(err.body(), err.status)


def _bearer(request: Request) -> str | None:
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    return credentials.strip() or None if scheme.lower() == "bearer" else None


def _int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ScimError(400, f"{name} must be an integer", "invalidValue") from None


def create_app(
    registry: IssuerRegistry,
    directory: Directory,
    scim_token_hashes: Mapping[str, str],
    *,
    required_group: str = "Finance",
) -> Starlette:
    """`scim_token_hashes` maps sha256(token) hex digests to the tenant the token provisions."""

    def scim_tenant(request: Request) -> str:
        token = _bearer(request)
        digest = hashlib.sha256(token.encode()).hexdigest() if token else ""
        tenant = scim_token_hashes.get(digest)
        if tenant is None:  # RFC 7644 6.1: the credential, not the URL, selects the tenant
            raise ScimError(401, "invalid SCIM bearer token")
        return tenant

    def current_user(request: Request, tenant: str) -> ScimUser | None:
        session = directory.sessions.get(request.cookies.get("sid", ""))
        if session is None or session.tenant != tenant:
            return None
        user = directory.users.get((tenant, session.user_id))
        return user if user is not None and user.active else None

    async def sso_callback(request: Request) -> Response:
        tenant = request.path_params["tenant"]
        body = await request.json()
        try:
            conn = registry.connection(tenant)
            principal = registry.validate(
                str(body.get("id_token", "")), tenant=tenant, audience=conn.client_id
            )
        except TokenRejectedError:
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        # Join on the immutable subject that SCIM stored as externalId, never on email.
        user = directory.user_for_subject(tenant, principal.subject)
        if user is None:
            return JSONResponse({"error": "not_provisioned"}, status_code=403)
        if not user.active:
            return JSONResponse({"error": "deactivated"}, status_code=403)
        response = JSONResponse({"user": user.id})
        response.set_cookie(
            "sid", directory.open_session(tenant, user.id), httponly=True, samesite="lax"
        )
        return response

    async def me(request: Request) -> Response:
        user = current_user(request, request.path_params["tenant"])
        if user is None:
            return JSONResponse({"error": "login_required"}, status_code=401)
        return JSONResponse({"id": user.id, "userName": user.user_name})

    async def reports(request: Request) -> Response:
        tenant = request.path_params["tenant"]
        user = current_user(request, tenant)
        if user is None:
            return JSONResponse({"error": "login_required"}, status_code=401)
        if not directory.is_member(tenant, user.id, required_group):  # read live, not cached
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({"reports": []})

    async def api_whoami(request: Request) -> Response:
        token = _bearer(request) or ""
        try:
            tenant = registry.tenant_for_issuer(token)
            conn = registry.connection(tenant)
            principal = registry.validate(token, tenant=tenant, audience=conn.api_audience)
        except TokenRejectedError:
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        # A valid JWT outlives deprovisioning, so gate every call on the SCIM directory.
        user = directory.user_for_subject(tenant, principal.subject)
        if user is None or not user.active:
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        return JSONResponse({"tenant": tenant, "user": user.id})

    async def users_collection(request: Request) -> Response:
        try:
            tenant = scim_tenant(request)
            if request.method == "POST":
                user = directory.create_user(tenant, await request.json())
                return _scim(user.to_scim(), 201, {"ETag": user.etag})
            found = directory.find_users(tenant, request.query_params.get("filter"))
            start = max(_int_param(request, "startIndex", 1), 1)  # RFC 7644 3.4.2.4: < 1 -> 1
            count = max(_int_param(request, "count", 100), 0)  # negative -> 0
        except ScimError as err:
            return _scim_error(err)
        page = found[start - 1 : start - 1 + count]
        return _scim(
            {
                "schemas": [LIST_SCHEMA],
                "totalResults": len(found),
                "startIndex": start,
                "itemsPerPage": len(page),
                "Resources": [u.to_scim() for u in page],
            }
        )

    async def user_item(request: Request) -> Response:
        user_id = request.path_params["user_id"]
        try:
            tenant = scim_tenant(request)
            if request.method == "DELETE":
                directory.delete_user(tenant, user_id)
                return Response(status_code=204)
            if request.method == "PATCH":
                user = directory.patch_user(
                    tenant, user_id, await request.json(), request.headers.get("if-match")
                )
            else:
                user = directory.get_user(tenant, user_id)
        except ScimError as err:
            return _scim_error(err)
        return _scim(user.to_scim(), 200, {"ETag": user.etag})

    async def groups_collection(request: Request) -> Response:
        try:
            group = directory.create_group(scim_tenant(request), await request.json())
        except ScimError as err:
            return _scim_error(err)
        return _scim(
            {"schemas": [GROUP_SCHEMA], "id": group.id, "displayName": group.display_name}, 201
        )

    async def group_item(request: Request) -> Response:
        try:
            tenant = scim_tenant(request)
            directory.patch_group(tenant, request.path_params["group_id"], await request.json())
        except ScimError as err:
            return _scim_error(err)
        return Response(status_code=204)

    return Starlette(
        routes=[
            Route("/t/{tenant}/sso/callback", sso_callback, methods=["POST"]),
            Route("/t/{tenant}/me", me, methods=["GET"]),
            Route("/t/{tenant}/reports", reports, methods=["GET"]),
            Route("/api/whoami", api_whoami, methods=["GET"]),
            Route("/scim/v2/Users", users_collection, methods=["GET", "POST"]),
            Route("/scim/v2/Users/{user_id}", user_item, methods=["GET", "PATCH", "DELETE"]),
            Route("/scim/v2/Groups", groups_collection, methods=["POST"]),
            Route("/scim/v2/Groups/{group_id}", group_item, methods=["PATCH"]),
        ]
    )
