"""Consumer-driven contract replay (Pact V3 rules) vs bi-directional OpenAPI comparison.

Two ways to answer "can this provider change ship without breaking a consumer":

* CDCT replay: every consumer interaction is replayed against the real provider app after
  its provider state is set up, and the actual response is matched with Pact V3 rules
  (extra response keys allowed, missing keys fail, matchers chosen by path weighting).
* BDCT comparison: no provider code runs; each consumer interaction is validated statically
  against the provider's OpenAPI document, transformed the way openapi-pact-comparator
  2.5.0 does (`additionalProperties: false` forced onto response objects, `required` dropped).
"""

import copy
import enum
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import httpx
from fastapi import FastAPI, HTTPException
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import BaseModel

type Json = Any
type Matcher = Mapping[str, Any]
type StateHandler = Callable[[Mapping[str, Any]], None]


# --- Pact V3 file model -------------------------------------------------------------------


@dataclass(frozen=True)
class Interaction:
    description: str
    provider_state: str | None
    state_params: Mapping[str, Any]
    method: str
    path: str
    request_body: Json | None
    status: int
    response_body: Json | None
    body_rules: Mapping[str, Sequence[Matcher]]


@dataclass(frozen=True)
class Pact:
    consumer: str
    provider: str
    interactions: tuple[Interaction, ...]


def parse_pact(doc: Mapping[str, Any]) -> Pact:
    """Read the subset of a Pact V3 file this lab uses: HTTP interactions and body rules."""
    interactions: list[Interaction] = []
    for raw in doc["interactions"]:
        states: list[dict[str, Any]] = raw.get("providerStates") or []
        if len(states) > 1:
            raise ValueError(f"{raw['description']!r}: lab supports at most one provider state")
        rules = raw["response"].get("matchingRules", {}).get("body", {})
        interactions.append(
            Interaction(
                description=raw["description"],
                provider_state=states[0]["name"] if states else None,
                state_params=states[0].get("params", {}) if states else {},
                method=raw["request"]["method"].upper(),
                path=raw["request"]["path"],
                request_body=raw["request"].get("body"),
                status=raw["response"]["status"],
                response_body=raw["response"].get("body"),
                body_rules={path: tuple(rule["matchers"]) for path, rule in rules.items()},
            )
        )
    return Pact(doc["consumer"]["name"], doc["provider"]["name"], tuple(interactions))


# --- Pact V3 matching: path weighting and body comparison -------------------------------------


class Wildcard(enum.Enum):
    STAR = "*"


type Segment = str | int | Wildcard
_SEGMENT = re.compile(r"\.(\*)|\[(\*)\]|\['([^']+)'\]|\[(\d+)\]|\.([^.\[\]]+)")


def parse_rule_path(expression: str) -> tuple[Segment, ...]:
    """Split a Pact body matcher path such as `$.users[*].email` into segments."""
    if not expression.startswith("$"):
        raise ValueError(f"matcher path must start with '$': {expression!r}")
    segments: list[Segment] = []
    position = 1
    while position < len(expression):
        token = _SEGMENT.match(expression, position)
        if token is None:
            raise ValueError(f"unsupported matcher path {expression!r} at {position}")
        star_dot, star_bracket, quoted, index, name = token.groups()
        if star_dot or star_bracket:
            segments.append(Wildcard.STAR)
        elif index is not None:
            segments.append(int(index))
        else:
            segments.append(quoted if quoted is not None else name)
        position = token.end()
    return tuple(segments)


def path_weight(rule: Sequence[Segment], actual: Sequence[str | int]) -> int:
    """Pact V3 matcher selection weight: root 2, exact key or index 2, star 1, mismatch 0."""
    if len(rule) > len(actual):
        return 0
    weight = 2
    for wanted, seen in zip(rule, actual, strict=False):
        if wanted is Wildcard.STAR:
            continue
        if type(wanted) is not type(seen) or wanted != seen:
            return 0
        weight *= 2
    return weight


def select_matchers(
    rules: Mapping[str, Sequence[Matcher]], actual: Sequence[str | int]
) -> Sequence[Matcher]:
    """Return the matchers of the highest-weighted rule path; rules cascade to children."""
    best: tuple[int, int] = (0, 0)
    chosen: Sequence[Matcher] = ()
    for expression, matchers in rules.items():
        segments = parse_rule_path(expression)
        score = (path_weight(segments, actual), len(segments))
        if score[0] > 0 and score > best:
            best, chosen = score, matchers
    return chosen


@dataclass(frozen=True)
class Mismatch:
    path: str
    message: str


def _render(path: Sequence[str | int]) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)


def _json_type(value: Json) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _scalar_ok(matcher: Matcher, expected: Json, actual: Json) -> bool:
    kind = matcher.get("match", "type" if "min" in matcher or "max" in matcher else None)
    match kind:
        case "equality":
            return _json_type(expected) == _json_type(actual) and expected == actual
        case "type":
            return _json_type(expected) == _json_type(actual)
        case "integer":
            return isinstance(actual, int) and not isinstance(actual, bool)
        case "number":
            return _json_type(actual) == "number"
        case "null":
            return actual is None
        case "regex":
            # Rust `Regex::is_match` (pact_matching) is an unanchored search, not a full match.
            return isinstance(actual, str | int | float) and bool(
                re.search(matcher["regex"], str(actual))
            )
        case "include":
            return isinstance(actual, str) and matcher["value"] in actual
        case _:
            raise ValueError(f"unsupported matcher {matcher!r}: refusing to pass silently")


def _as_list(value: Json) -> list[Any]:
    """Type a JSON array for the checkers; callers have already tested `isinstance(list)`."""
    return cast("list[Any]", value)


def match_body(
    expected: Json,
    actual: Json,
    rules: Mapping[str, Sequence[Matcher]],
    *,
    allow_unexpected_keys: bool,
    path: tuple[str | int, ...] = (),
) -> list[Mismatch]:
    """Compare an actual body with a Pact expectation (V2/V3 body semantics)."""
    where = _render(path)
    matchers = select_matchers(rules, path)

    def child(want: Json, got: Json, step: str | int) -> list[Mismatch]:
        return match_body(
            want, got, rules, allow_unexpected_keys=allow_unexpected_keys, path=(*path, step)
        )

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [Mismatch(where, f"expected an object but got {_json_type(actual)}")]
        want_map = cast("dict[str, Any]", expected)
        got_map = cast("dict[str, Any]", actual)
        found: list[Mismatch] = []
        if not allow_unexpected_keys:
            found += [
                Mismatch(where, f"unexpected key {k!r}") for k in got_map if k not in want_map
            ]
        for key, value in want_map.items():
            if key not in got_map:
                found.append(Mismatch(where, f"expected key {key!r} but it was missing"))
            else:
                found += child(value, got_map[key], key)
        return found
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [Mismatch(where, f"expected an array but got {_json_type(actual)}")]
        want_list = _as_list(expected)
        got_list = _as_list(actual)
        sized = [m for m in matchers if m.get("match", "type") == "type"]
        if sized and want_list:
            low, high = sized[0].get("min", 0), sized[0].get("max")
            found = []
            if len(got_list) < low or (high is not None and len(got_list) > high):
                found.append(
                    Mismatch(where, f"array length {len(got_list)} outside [{low}, {high}]")
                )
            for index, item in enumerate(got_list):
                found += child(want_list[0], item, index)  # every item vs the first example
            return found
        if len(got_list) != len(want_list):
            return [Mismatch(where, f"expected {len(want_list)} items but got {len(got_list)}")]
        found = []
        for index, (want, got) in enumerate(zip(want_list, got_list, strict=True)):
            found += child(want, got, index)
        return found
    active = matchers or ({"match": "equality"},)
    failed = [m for m in active if not _scalar_ok(m, expected, actual)]
    return [Mismatch(where, f"{actual!r} failed {dict(m)}") for m in failed]


# --- CDCT: replay each consumer interaction against the running provider -------------------


@dataclass(frozen=True)
class InteractionResult:
    description: str
    mismatches: tuple[Mismatch, ...]


@dataclass(frozen=True)
class VerificationResult:
    consumer: str
    results: tuple[InteractionResult, ...]

    @property
    def ok(self) -> bool:
        return all(not r.mismatches for r in self.results)

    def failures(self) -> dict[str, list[str]]:
        return {
            r.description: [f"{m.path}: {m.message}" for m in r.mismatches]
            for r in self.results
            if r.mismatches
        }


async def verify_pact(
    pact: Pact,
    app: FastAPI,
    state_handlers: Mapping[str, StateHandler],
    reset: Callable[[], None],
) -> VerificationResult:
    """Provider verification: reset, set the provider state, replay, match the response."""
    results: list[InteractionResult] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for item in pact.interactions:
            reset()
            found: list[Mismatch] = []
            if item.provider_state is not None:
                handler = state_handlers.get(item.provider_state)
                if handler is None:
                    found.append(Mismatch("state", f"no handler for {item.provider_state!r}"))
                    results.append(InteractionResult(item.description, tuple(found)))
                    continue
                handler(item.state_params)
            response = await client.request(item.method, item.path, json=item.request_body)
            if response.status_code != item.status:
                found.append(
                    Mismatch("status", f"expected {item.status}, got {response.status_code}")
                )
            elif item.response_body is not None:
                found += match_body(
                    item.response_body,
                    response.json(),
                    item.body_rules,
                    allow_unexpected_keys=True,
                )
            results.append(InteractionResult(item.description, tuple(found)))
    return VerificationResult(pact.consumer, tuple(results))


# --- BDCT: compare consumer interactions with the provider's OpenAPI document --------------

type Severity = Literal["error", "warning"]
_SUBSCHEMA_MAPS = ("properties", "patternProperties", "$defs", "dependentSchemas")
_SUBSCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SUBSCHEMA_ONE = (
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    interaction: str
    detail: str = ""


def schema_errors(schema: Json, instance: Json) -> list[str]:
    """Every JSON Schema 2020-12 validation error message, in jsonschema's order."""
    validator: Any = Draft202012Validator(schema)
    return [str(error.message) for error in validator.iter_errors(instance)]


def _walk(schema: Json, visit: Callable[[dict[str, Any]], None]) -> None:
    """Visit every subschema (schema-aware: never treats a `properties` map as a schema)."""
    if not isinstance(schema, dict):
        return
    node = cast("dict[str, Any]", schema)
    visit(node)
    for key in _SUBSCHEMA_MAPS:
        for child in cast("dict[str, Any]", node.get(key, {})).values():
            _walk(child, visit)
    for key in _SUBSCHEMA_LISTS:
        for child in _as_list(node.get(key, [])):
            _walk(child, visit)
    for key in _SUBSCHEMA_ONE:
        _walk(node.get(key), visit)


def inline_refs(schema: Json, spec: Mapping[str, Any], seen: tuple[str, ...] = ()) -> Json:
    """Replace local `#/components/...` references with copies of their targets."""
    if isinstance(schema, list):
        return [inline_refs(item, spec, seen) for item in _as_list(schema)]
    if not isinstance(schema, dict):
        return schema
    node = cast("dict[str, Any]", schema)
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in seen:
            raise ValueError(f"recursive $ref {ref} is not supported by this lab")
        target: Any = spec
        for part in ref.removeprefix("#/").split("/"):
            target = target[part]
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        return inline_refs({**copy.deepcopy(target), **siblings}, spec, (*seen, ref))
    return {key: inline_refs(value, spec, seen) for key, value in node.items()}


def transform_received_schema(schema: Json) -> Json:
    """openapi-pact-comparator 2.5.0 src/transform/responseSchema.ts, transformReceivedSchema."""
    result = copy.deepcopy(schema)

    def close(node: dict[str, Any]) -> None:
        composed = any(k in node for k in ("oneOf", "allOf", "anyOf"))
        is_open = node.get("additionalProperties", True) is True
        if node.get("type") == "object" and is_open and not composed:
            node["additionalProperties"] = False

    def relax(node: dict[str, Any]) -> None:
        node.pop("minProperties", None)
        if "oneOf" not in node:
            node.pop("required", None)

    _walk(result, close)
    _walk(result, relax)
    return result


def _route(spec: Mapping[str, Any], method: str, path: str) -> Mapping[str, Any] | None:
    templates = sorted(spec.get("paths", {}), key=lambda t: t.count("{"))  # static paths first
    for template in templates:
        literals = re.split(r"\{[^/}]+\}", template)
        if re.fullmatch("[^/]+".join(re.escape(part) for part in literals), path):
            operation: Mapping[str, Any] | None = spec["paths"][template].get(method.lower())
            return operation
    return None


def _json_schema(container: Mapping[str, Any] | None) -> Json | None:
    content = (container or {}).get("content", {})
    media = content.get("application/json")
    return None if media is None else media.get("schema")


def compare_pact_to_openapi(pact: Pact, spec: Mapping[str, Any]) -> list[Finding]:
    """Static cross-contract check: is every consumer interaction a subset of the spec?"""
    findings: list[Finding] = []
    for item in pact.interactions:
        name = item.description
        operation = _route(spec, item.method, item.path)
        if operation is None:
            findings.append(Finding("request.path-or-method.unknown", "error", name))
            continue
        responses: Mapping[str, Any] = operation.get("responses", {})
        exact, ranged = str(item.status), f"{item.status // 100}XX"
        response = responses.get(exact) or responses.get(ranged) or responses.get("default")
        if response is None:
            findings.append(Finding("response.status.unknown", "error", name, exact))
            continue
        if exact not in responses and ranged not in responses:
            findings.append(Finding("response.status.default", "warning", name, exact))
        request_schema = _json_schema(operation.get("requestBody"))
        if item.status < 400 and item.request_body is not None and request_schema is not None:
            for message in schema_errors(inline_refs(request_schema, spec), item.request_body):
                findings.append(Finding("request.body.incompatible", "error", name, message))
        schema = _json_schema(response)
        if item.response_body is None:
            continue
        if schema is None:
            findings.append(Finding("response.body.unknown", "error", name))
            continue
        closed = transform_received_schema(inline_refs(schema, spec))
        for message in schema_errors(closed, item.response_body):
            findings.append(Finding("response.body.incompatible", "error", name, message))
    return findings


async def self_verify(
    app: FastAPI, spec: Mapping[str, Any], probes: Sequence[tuple[str, str]]
) -> list[str]:
    """Provider-side half of BDCT: real responses must conform to the untransformed spec."""
    problems: list[str] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in probes:
            response = await client.request(method, path)
            operation = _route(spec, method, path)
            declared = (operation or {}).get("responses", {}).get(str(response.status_code))
            schema = _json_schema(declared)
            if schema is None:
                problems.append(f"{method} {path}: {response.status_code} not declared")
                continue
            body = response.json()
            problems += [
                f"{method} {path}: {m}" for m in schema_errors(inline_refs(schema, spec), body)
            ]
    return problems


# --- The provider: one FastAPI app, three releases -------------------------------------------

type Release = Literal["v1", "v2-removed", "v2-optional-unpopulated"]


@dataclass
class UserStore:
    users: dict[int, dict[str, str]] = field(default_factory=dict[int, dict[str, str]])

    def clear(self) -> None:
        self.users.clear()


class ErrorOut(BaseModel):
    detail: str


class UserIn(BaseModel):
    name: str
    email: str


class UserV1(BaseModel):
    id: int
    name: str
    email: str


class UserV2Removed(BaseModel):
    id: int
    name: str


class UserV2Optional(BaseModel):
    id: int
    name: str
    email: str | None = None


def build_provider(release: Release, store: UserStore) -> FastAPI:
    """`user-service` at a given release; the OpenAPI document comes from `app.openapi()`."""
    app = FastAPI(title="user-service", version=release)
    models: dict[str, type[BaseModel]] = {
        "v1": UserV1,
        "v2-removed": UserV2Removed,
        "v2-optional-unpopulated": UserV2Optional,
    }
    model = models[release]

    def render(user_id: int, record: Mapping[str, str]) -> dict[str, Any]:
        if release == "v1":
            return {"id": user_id, **record}
        return {"id": user_id, "name": record["name"]}  # v2 stops sending email

    @app.get(
        "/users/{user_id}",
        response_model=model,
        response_model_exclude_none=True,
        responses={404: {"model": ErrorOut}},
    )
    async def get_user(user_id: int) -> dict[str, Any]:
        record = store.users.get(user_id)
        if record is None:
            raise HTTPException(status_code=404, detail="user not found")
        return render(user_id, record)

    @app.post("/users", status_code=201, response_model=model, response_model_exclude_none=True)
    async def create_user(body: UserIn) -> dict[str, Any]:
        user_id = max(store.users, default=0) + 1
        store.users[user_id] = {"name": body.name, "email": body.email}
        return render(user_id, store.users[user_id])

    return app


def state_handlers(store: UserStore) -> dict[str, StateHandler]:
    def user_exists(params: Mapping[str, Any]) -> None:
        store.users[int(params["id"])] = {"name": "Ada", "email": "ada@example.com"}

    def no_users(params: Mapping[str, Any]) -> None:
        del params  # the precondition is the empty store that `reset` produced

    return {"a user exists": user_exists, "no users exist": no_users}


# --- Two consumers of the same provider, as Pact V3 files ----------------------------------

_USER_RULES = {
    "$.id": {"matchers": [{"match": "integer"}]},
    "$.name": {"matchers": [{"match": "type"}]},
}

BILLING_PACT: dict[str, Any] = {
    "consumer": {"name": "billing"},
    "provider": {"name": "user-service"},
    "interactions": [
        {
            "description": "get user 1 to address an invoice",
            "providerStates": [{"name": "a user exists", "params": {"id": 1}}],
            "request": {"method": "GET", "path": "/users/1"},
            "response": {
                "status": 200,
                "body": {"id": 1, "name": "Ada", "email": "ada@example.com"},
                "matchingRules": {
                    "body": {
                        **_USER_RULES,
                        "$.email": {"matchers": [{"match": "regex", "regex": "^[^@]+@[^@]+$"}]},
                    }
                },
            },
        }
    ],
    "metadata": {"pactSpecification": {"version": "3.0.0"}},
}

DASHBOARD_PACT: dict[str, Any] = {
    "consumer": {"name": "dashboard"},
    "provider": {"name": "user-service"},
    "interactions": [
        {
            "description": "get user 1 to show a name",
            "providerStates": [{"name": "a user exists", "params": {"id": 1}}],
            "request": {"method": "GET", "path": "/users/1"},
            "response": {
                "status": 200,
                "body": {"id": 1, "name": "Ada"},
                "matchingRules": {"body": _USER_RULES},
            },
        },
        {
            "description": "get a missing user",
            "providerStates": [{"name": "no users exist"}],
            "request": {"method": "GET", "path": "/users/1"},
            "response": {"status": 404, "body": {"detail": "user not found"}},
        },
    ],
    "metadata": {"pactSpecification": {"version": "3.0.0"}},
}
