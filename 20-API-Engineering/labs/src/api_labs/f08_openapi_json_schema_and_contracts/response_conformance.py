"""Zero-tolerance response conformance: every test response is checked against the OpenAPI contract.

`OpenAPIContract` resolves a concrete request to its operation (OpenAPI 3.1.2 Section 4.8.8),
selects the Response Object for the status code (exact code, then `NXX` range, then `default`,
Section 4.8.16), selects the Media Type Object for `Content-Type` (most specific key wins,
Section 4.8.17), and then checks headers, framing, JSON syntax, and the JSON Schema 2020-12 body.
`conformance_hook` runs that check on every `httpx.AsyncClient` response, so a test cannot
receive a response that breaks the contract without failing.
"""

import json
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]
from jsonschema.validators import extend  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

type Rule = Literal["operation", "status", "content-type", "framing", "header", "json", "body"]
type Json = Any

CONTRACT_URI = "urn:api-labs:openapi-contract"
WAIVER_EXTENSION = "contract_waiver"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
# OpenAPI Format Registry names that constrain nothing a JSON Schema checker could verify.
ANNOTATION_ONLY_FORMATS = frozenset({"int32", "int64", "float", "double", "password"})


@dataclass(frozen=True, slots=True)
class Violation:
    """One broken clause of the contract, named by the rule it breaks."""

    rule: Rule
    detail: str


class ContractViolationError(AssertionError):
    """Raised from the response hook; an `AssertionError` so pytest reports it as a failure."""

    def __init__(self, method: str, path: str, status: int, violations: tuple[Violation, ...]):
        self.violations = violations
        lines = "\n".join(f"  [{v.rule}] {v.detail}" for v in violations)
        super().__init__(f"{method} {path} -> {status} breaks the OpenAPI contract:\n{lines}")


@dataclass(frozen=True, slots=True)
class OperationRef:
    method: str
    path_template: str
    operation: Mapping[str, Json]


@dataclass(frozen=True, slots=True)
class CheckResult:
    operation: OperationRef | None
    status_key: str | None
    violations: tuple[Violation, ...]


def escape_pointer_token(token: str) -> str:
    """RFC 6901 Section 3: `~` becomes `~0` and `/` becomes `~1`, in that order."""
    return token.replace("~", "~0").replace("/", "~1")


def media_type_essence(value: str) -> str:
    """`type/subtype` without parameters, lowercased (RFC 9110 Section 8.3.1)."""
    return value.split(";", 1)[0].strip().lower()


def is_json_media_type(essence: str) -> bool:
    """`application/json` or any `+json` structured syntax suffix (RFC 6839 Section 3.1)."""
    return essence == "application/json" or essence.endswith("+json")


def select_media_type(content: Mapping[str, Json], actual: str) -> str | None:
    """Return the most specific `content` key for `actual`: exact, then `type/*`, then `*/*`."""
    by_essence = {media_type_essence(key): key for key in content}
    essence = media_type_essence(actual)
    main_type = essence.split("/", 1)[0]
    for candidate in (essence, f"{main_type}/*", "*/*"):
        if candidate in by_essence:
            return by_essence[candidate]
    return None


def select_response_key(responses: Mapping[str, Json], status: int) -> str | None:
    """An explicit code beats its range (OpenAPI 3.1.2 Section 4.8.16.2); `default` is last."""
    for key in (str(status), f"{status // 100}XX", "default"):
        if key in responses:
            return key
    return None


def _template_regex(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^{}]+\})", template)
    pattern = "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts)
    return re.compile(f"^{pattern}$")


def _reject_constant(token: str) -> float:
    raise ValueError(f"{token} is not a JSON number (RFC 8259 Section 6)")


def _reject_duplicate_keys(pairs: list[tuple[str, Json]]) -> dict[str, Json]:
    result: dict[str, Json] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object member {key!r} (RFC 8259 Section 4)")
        result[key] = value
    return result


def parse_strict_json(body: bytes) -> Json:
    """Decode as UTF-8 without BOM stripping and reject `NaN`, `Infinity`, and duplicate keys."""
    text = body.decode("utf-8")
    return json.loads(
        text, parse_constant=_reject_constant, object_pairs_hook=_reject_duplicate_keys
    )


def _write_only_in_response(
    validator: Any, value: Json, instance: Json, schema: Mapping[str, Json]
) -> Iterator[Any]:
    """`writeOnly: true` means 'never present when retrieved' (JSON Schema Validation 9.4)."""
    del validator, instance, schema
    if value is True:
        yield ValidationError("writeOnly value is present in a response")


ResponseValidator: Any = cast(
    "Any", extend(Draft202012Validator, {"writeOnly": _write_only_in_response})
)


def formats_in(node: Json) -> set[str]:
    """Every string value of a `format` keyword anywhere in the document."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in cast("dict[str, Json]", node).items():
            if key == "format" and isinstance(value, str):
                found.add(value)
            else:
                found |= formats_in(value)
    elif isinstance(node, list):
        for item in cast("list[object]", node):
            found |= formats_in(item)
    return found


def checkable_formats() -> frozenset[str]:
    """Formats this environment can assert; some exist only if optional packages are installed."""
    checker: Any = Draft202012Validator.FORMAT_CHECKER
    return frozenset(cast("dict[str, Any]", checker.checkers))


def _coerce_header(value: str, schema: Mapping[str, Json]) -> Json:
    """Header values are strings on the wire; coerce scalars before schema validation."""
    kind = schema.get("type")
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
    except ValueError:
        return value
    if kind == "boolean" and value in {"true", "false"}:
        return value == "true"
    return value


@dataclass
class OpenAPIContract:
    """A compiled OpenAPI 3.1 document that judges responses."""

    document: Mapping[str, Json]
    base_path: str = ""
    assert_formats: bool = True
    _routes: list[tuple[re.Pattern[str], str]] = field(init=False, repr=False)
    _registry: Registry[Json] = field(init=False, repr=False)
    _validators: dict[str, Any] = field(init=False, repr=False, default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if self.assert_formats:
            # jsonschema treats an unknown or uncheckable format as valid; fail closed instead.
            unchecked = formats_in(self.document) - checkable_formats() - ANNOTATION_ONLY_FORMATS
            if unchecked:
                raise ValueError(f"format assertion has no checker for {sorted(unchecked)}")
        paths: Mapping[str, Json] = self.document.get("paths", {})
        # Section 4.8.8.1: concrete paths are matched before their templated counterparts.
        ordered = sorted(paths, key=lambda p: (p.count("{"), -len(p.split("/"))))
        self._routes = [(_template_regex(p), p) for p in ordered]
        resource: Resource[Json] = Resource.from_contents(
            dict(self.document), default_specification=DRAFT202012
        )
        # A registry without `retrieve` never touches the network for a remote `$ref`.
        empty: Registry[Json] = Registry()
        self._registry = empty.with_resource(CONTRACT_URI, resource)

    def documented_responses(self) -> set[tuple[str, str, str]]:
        """Every (METHOD, path template, response key) pair the document promises."""
        pairs: set[tuple[str, str, str]] = set()
        for template, item in self.document.get("paths", {}).items():
            for method, operation in item.items():
                if method in HTTP_METHODS:
                    for key in operation.get("responses", {}):
                        pairs.add((method.upper(), template, key))
        return pairs

    def find_operation(self, method: str, path: str) -> OperationRef | None:
        if self.base_path:
            if not path.startswith(self.base_path):
                return None
            path = path[len(self.base_path) :] or "/"
        for regex, template in self._routes:
            if regex.match(path):
                item = self.document["paths"][template]
                operation = item.get(method.lower())
                if operation is not None:
                    return OperationRef(method.upper(), template, operation)
        return None

    def _validator(self, pointer: str) -> Any:
        """One compiled validator per schema location, built on first use and then reused."""
        validator = self._validators.get(pointer)
        if validator is None:
            validator = ResponseValidator(
                {"$ref": f"{CONTRACT_URI}#{pointer}"},
                registry=self._registry,
                format_checker=Draft202012Validator.FORMAT_CHECKER if self.assert_formats else None,
            )
            self._validators[pointer] = validator
        return validator

    def _schema_errors(self, pointer: str, instance: Json) -> list[str]:
        errors = cast("Iterator[Any]", self._validator(pointer).iter_errors(instance))
        found: list[str] = []
        for error in sorted(errors, key=lambda e: list(e.absolute_path)):
            location = "/".join(str(p) for p in error.absolute_path) or "<root>"
            found.append(f"{location}: {error.message}")
        return found

    def check(
        self, method: str, path: str, status: int, headers: httpx.Headers, body: bytes
    ) -> CheckResult:
        op = self.find_operation(method, path)
        if op is None:
            detail = f"no operation documents {method.upper()} {path}"
            return CheckResult(None, None, (Violation("operation", detail),))
        responses: Mapping[str, Json] = op.operation.get("responses", {})
        status_key = select_response_key(responses, status)
        if status_key is None:
            detail = f"status {status} is not documented; documented: {sorted(responses)}"
            return CheckResult(op, None, (Violation("status", detail),))
        response: Mapping[str, Json] = responses[status_key]
        template, key = escape_pointer_token(op.path_template), escape_pointer_token(status_key)
        base = f"/paths/{template}/{method.lower()}/responses/{key}"
        violations = [*self._check_headers(response, headers, base)]
        violations.extend(self._check_body(method, status, response, headers, body, base))
        return CheckResult(op, status_key, tuple(violations))

    def _check_headers(
        self, response: Mapping[str, Json], headers: httpx.Headers, base: str
    ) -> Iterator[Violation]:
        for name, spec in response.get("headers", {}).items():
            if name.lower() == "content-type":
                continue  # Section 4.8.17.1: a documented Content-Type header SHALL be ignored.
            value = headers.get(name)
            if value is None:
                if spec.get("required", False):
                    yield Violation("header", f"required header {name} is missing")
                continue
            schema = spec.get("schema")
            if schema is None:
                continue
            pointer = f"{base}/headers/{escape_pointer_token(name)}/schema"
            for message in self._schema_errors(pointer, _coerce_header(value, schema)):
                yield Violation("header", f"{name}={value!r}: {message}")

    def _check_body(
        self,
        method: str,
        status: int,
        response: Mapping[str, Json],
        headers: httpx.Headers,
        body: bytes,
        base: str,
    ) -> Iterator[Violation]:
        if status < 200 or status in {204, 304}:
            if body:
                yield Violation("framing", f"status {status} must not carry content")
            return
        if method.upper() == "HEAD":
            return
        content: Mapping[str, Json] = response.get("content", {})
        if not content:
            if body:
                yield Violation("content-type", "response documents no content but has a body")
            return
        content_type = headers.get("content-type")
        if content_type is None:
            yield Violation("content-type", "body is present but Content-Type is missing")
            return
        key = select_media_type(content, content_type)
        if key is None:
            yield Violation(
                "content-type", f"{content_type!r} is not documented; documented: {sorted(content)}"
            )
            return
        if not is_json_media_type(media_type_essence(key)):
            return
        try:
            instance = parse_strict_json(body)
        except ValueError as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors.
            yield Violation("json", f"body is not strict UTF-8 JSON: {exc}")
            return
        if "schema" not in content[key]:
            return
        pointer = f"{base}/content/{escape_pointer_token(key)}/schema"
        for message in self._schema_errors(pointer, instance):
            yield Violation("body", message)


@dataclass
class ConformanceRecorder:
    """Session ledger: which documented responses were exercised, and which checks were waived."""

    observed: set[tuple[str, str, str]] = field(default_factory=set[tuple[str, str, str]])
    waived: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    def record(self, result: CheckResult) -> None:
        if result.operation is not None and result.status_key is not None:
            op = result.operation
            self.observed.add((op.method, op.path_template, result.status_key))

    def uncovered(self, contract: OpenAPIContract) -> set[tuple[str, str, str]]:
        return contract.documented_responses() - self.observed


def conformance_hook(
    contract: OpenAPIContract, recorder: ConformanceRecorder
) -> Callable[[httpx.Response], Awaitable[None]]:
    """An httpx response event hook that fails the request when the response breaks the contract.

    Hooks run before the body is read, so the hook reads it; httpx then closes the response and
    re-raises when a hook raises (httpx 0.28.1 `AsyncClient._send_handling_redirects`).
    A request can opt out only by naming a reason in `extensions={"contract_waiver": reason}`,
    and every waiver is recorded.
    """

    async def check_response(response: httpx.Response) -> None:
        request = response.request
        waiver = request.extensions.get(WAIVER_EXTENSION)
        if isinstance(waiver, str) and waiver:
            recorder.waived.append((request.method, request.url.path, waiver))
            return
        body = await response.aread()
        result = contract.check(
            request.method, request.url.path, response.status_code, response.headers, body
        )
        recorder.record(result)
        if result.violations:
            raise ContractViolationError(
                request.method, request.url.path, response.status_code, result.violations
            )

    return check_response


# --- the application under test ----------------------------------------------------------------

type Defect = Literal[
    "undocumented-404",
    "leaked-field",
    "nan-price",
    "text-content-type",
    "missing-location",
    "body-on-204",
    "missing-field",
]


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    price: float = Field(ge=0)
    supplier_token: str = Field(json_schema_extra={"writeOnly": True})


class ItemOut(BaseModel):
    """Closed response model: `extra='forbid'` publishes `additionalProperties: false`."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    price: float


class NotFound(BaseModel):
    detail: str


def build_inventory_app(defects: frozenset[Defect] = frozenset()) -> FastAPI:
    """A small inventory API; each `defect` breaks exactly one clause of its own contract."""

    store: dict[int, dict[str, Json]] = {1: {"id": 1, "name": "bolt", "price": 0.25}}
    app = FastAPI(title="inventory", version="1.0.0")
    not_found_docs: dict[int | str, dict[str, Any]] = (
        {} if "undocumented-404" in defects else {404: {"model": NotFound}}
    )
    location_doc: dict[int | str, dict[str, Any]] = {
        201: {
            "headers": {
                "Location": {"required": True, "schema": {"type": "string", "pattern": "^/items/"}}
            }
        }
    }

    @app.get("/items/{item_id}", response_model=ItemOut, responses=not_found_docs)
    async def get_item(item_id: int) -> Any:
        item = store.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="item not found")
        if "leaked-field" in defects:
            return JSONResponse({**item, "supplier_cost": 0.11})
        if "nan-price" in defects:
            return {**item, "price": float("nan")}
        if "text-content-type" in defects:
            return Response(json.dumps(item), media_type="text/plain")
        if "missing-field" in defects:
            return {"id": item["id"], "name": item["name"]}
        return item

    @app.post("/items", status_code=201, response_model=ItemOut, responses=location_doc)
    async def create_item(body: ItemIn, response: Response) -> Any:
        item_id = max(store) + 1
        store[item_id] = {"id": item_id, "name": body.name, "price": body.price}
        if "missing-location" not in defects:
            response.headers["Location"] = f"/items/{item_id}"
        return store[item_id]

    @app.delete("/items/{item_id}", status_code=204, responses=not_found_docs)
    async def delete_item(item_id: int) -> Response:
        if store.pop(item_id, None) is None:
            raise HTTPException(status_code=404, detail="item not found")
        return Response(b"{}" if "body-on-204" in defects else b"", status_code=204)

    return app
