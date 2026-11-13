"""A Spectral-semantics OpenAPI style linter: four house rules, $ref-aware paths, exit codes."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Literal, cast

type JsonPath = tuple[str | int, ...]
type Report = Literal["use-site", "definition"]

# Spectral DiagnosticSeverity numbering: a lower number is more severe.
# Both `spectral lint --fail-severity` and `vacuum lint --fail-severity` fail the run
# when any result has severity <= the threshold.


class Severity(IntEnum):
    ERROR = 0
    WARN = 1
    INFO = 2
    HINT = 3


# OpenAPI 3.1 Path Item fixed operation fields; 3.2 adds `query` and `additionalOperations`.
METHODS_31 = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
METHODS_32 = (*METHODS_31, "query")
# The `#OperationObject` alias in @stoplight/spectral-rulesets 1.23.0 lists exactly METHODS_31.
SPECTRAL_OPERATION_ALIAS = METHODS_31

ERROR_STATUS = re.compile(r"^[45]")  # "404", "4XX", "500", "5XX"
PROBLEM_JSON = "application/problem+json"
PAGINATION_PARAMS = ("limit", "cursor")
# "id", "order_id", "order-id", "orderId", "orderID", "ID"; not "bid", "paid", "grid".
IDENTIFIER_NAME = re.compile(r"^(?:id|ID|Id)$|[_-](?:id|ID|Id)$|(?<=[a-z0-9])(?:Id|ID)$")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    path: JsonPath
    code: str
    message: str
    severity: Severity


class RefError(ValueError):
    """A `$ref` that is remote, dangling, or circular."""


def _pointer_tokens(ref: str) -> JsonPath:
    if not ref.startswith("#/"):
        raise RefError(f"refusing non-local $ref {ref!r}: the linter never fetches")
    return tuple(t.replace("~1", "/").replace("~0", "~") for t in ref[2:].split("/"))


def resolve(doc: Mapping[str, Any], node: Any) -> tuple[Any, JsonPath | None]:
    """Follow local `$ref` chains; return the target and the path of the final definition."""
    seen: set[str] = set()
    where: JsonPath | None = None
    while isinstance(node, Mapping) and "$ref" in node:
        ref = str(cast("Mapping[str, Any]", node)["$ref"])
        if ref in seen:
            raise RefError(f"circular $ref chain through {ref!r}")
        seen.add(ref)
        where = _pointer_tokens(ref)
        node = doc
        for token in where:
            if not isinstance(node, Mapping) or token not in node:
                raise RefError(f"dangling $ref {ref!r}")
            node = cast("Mapping[str, Any]", node)[token]
    return node, where


@dataclass(frozen=True, slots=True)
class Operation:
    template: str
    method: str
    path: JsonPath
    node: Mapping[str, Any]
    item: Mapping[str, Any]


def operations(doc: Mapping[str, Any], methods: tuple[str, ...]) -> Iterator[Operation]:
    """Select every Operation Object, like a `$.paths[*][get,put,...]` given."""
    for template, item in cast("Mapping[str, Any]", doc.get("paths") or {}).items():
        if not isinstance(item, Mapping):
            continue
        path_item = cast("Mapping[str, Any]", item)
        for method in methods:
            op = path_item.get(method)
            if isinstance(op, Mapping):
                yield Operation(template, method, ("paths", template, method), op, path_item)
        if "query" in methods:  # OpenAPI 3.2 `additionalOperations`: custom methods by name
            extra = cast("Mapping[str, Any]", path_item.get("additionalOperations") or {})
            for method, op in extra.items():
                if isinstance(op, Mapping):
                    where: JsonPath = ("paths", template, "additionalOperations", method)
                    node = cast("Mapping[str, Any]", op)
                    yield Operation(template, method, where, node, path_item)


class Linter:
    """Runs the four house rules and maps result paths the way the chosen engine does."""

    def __init__(self, doc: Mapping[str, Any], *, report: Report = "use-site") -> None:
        self.doc = doc
        self.report = report
        version = str(doc.get("openapi", ""))
        self.methods = METHODS_32 if version.startswith("3.2") else METHODS_31
        self.results: list[Diagnostic] = []

    def _emit(
        self, code: str, sev: Severity, msg: str, use: JsonPath, definition: JsonPath | None
    ) -> None:
        # Spectral (resolved rules) reports a finding inside a $ref target at the target's
        # own location and dedupes on code+path+message; vacuum keeps the use site.
        path = definition if self.report == "definition" and definition is not None else use
        diag = Diagnostic(path, code, msg, sev)
        if diag not in self.results:
            self.results.append(diag)

    def run(self, methods: tuple[str, ...] | None = None) -> list[Diagnostic]:
        seen_ids: dict[str, JsonPath] = {}
        for op in operations(self.doc, methods or self.methods):
            self._operation_id(op, seen_ids)
            self._problem_json(op)
            self._pagination(op)
            self._numeric_ids(op)
        return sorted(self.results, key=lambda d: (tuple(map(str, d.path)), d.code, d.message))

    def _operation_id(self, op: Operation, seen: dict[str, JsonPath]) -> None:
        value = op.node.get("operationId")
        where = (*op.path, "operationId")
        if not value:  # Spectral `truthy`: missing, "", null, false and 0 all fail
            self._emit("acme-operation-id", Severity.ERROR, "operationId is required", where, None)
        elif str(value) in seen:  # OAS 3.2.0 Section 4.10.1: unique and case-sensitive
            first = "/".join(map(str, seen[str(value)]))
            msg = f"operationId {value!r} already used at {first}"
            self._emit("acme-operation-id", Severity.ERROR, msg, where, None)
        else:
            seen[str(value)] = op.path

    def _problem_json(self, op: Operation) -> None:
        responses = cast("Mapping[str, Any]", op.node.get("responses") or {})
        for status, raw in responses.items():
            if not (ERROR_STATUS.match(status) or status == "default"):
                continue
            response, definition = resolve(self.doc, raw)
            content = cast("Mapping[str, Any]", response.get("content") or {})
            if PROBLEM_JSON not in content:
                msg = f"{status} response must offer {PROBLEM_JSON} (RFC 9457 Section 3)"
                use = (*op.path, "responses", status)
                self._emit("acme-error-problem-json", Severity.ERROR, msg, use, definition)

    def _parameters(self, op: Operation) -> Iterator[tuple[Mapping[str, Any], JsonPath, Any]]:
        """Effective parameters: operation entries override path-item ones by (name, in)."""
        merged: dict[tuple[str, str], tuple[Mapping[str, Any], JsonPath, Any]] = {}
        sources = ((op.item, ("paths", op.template)), (op.node, op.path))
        for owner, owner_path in sources:
            for index, raw in enumerate(cast("list[Any]", owner.get("parameters") or [])):
                param, definition = resolve(self.doc, raw)
                key = (str(param.get("name")), str(param.get("in")))
                merged[key] = (param, (*owner_path, "parameters", index), definition)
        yield from merged.values()

    def _pagination(self, op: Operation) -> None:
        if op.method != "get" or op.template.endswith("}"):
            return  # only collection GETs: the last path segment is not a template
        params = self._parameters(op)
        declared = {str(p.get("name")) for p, _, _ in params if p.get("in") == "query"}
        for name in PAGINATION_PARAMS:
            if name not in declared:
                msg = f"GET {op.template} is a collection and must declare query parameter {name!r}"
                self._emit("acme-collection-pagination", Severity.WARN, msg, op.path, None)

    def _numeric_ids(self, op: Operation) -> None:
        for param, use, definition in self._parameters(op):
            if is_identifier(str(param.get("name", ""))) and allows_integer(param.get("schema")):
                msg = f"identifier parameter {param.get('name')!r} must not be an integer"
                target = None if definition is None else (*definition, "schema", "type")
                code = "acme-no-numeric-ids"
                self._emit(code, Severity.ERROR, msg, (*use, "schema", "type"), target)


def is_identifier(name: str) -> bool:
    return IDENTIFIER_NAME.search(name) is not None


def allows_integer(schema: Any) -> bool:
    """True when `type` is "integer" or an OpenAPI 3.1 type array that contains it."""
    if not isinstance(schema, Mapping):
        return False
    kind = cast("Mapping[str, Any]", schema).get("type")
    return kind == "integer" or (isinstance(kind, list) and "integer" in kind)


def lint(doc: Mapping[str, Any], *, report: Report = "use-site") -> list[Diagnostic]:
    return Linter(doc, report=report).run()


def exit_code(results: list[Diagnostic], fail_severity: Severity = Severity.ERROR) -> int:
    """`severeEnoughToFail`: exit 1 when any result is at or above the threshold."""
    return 1 if any(d.severity <= fail_severity for d in results) else 0


# Models of the two published OWASP "no numeric IDs" rules, matching their observed output.


def spectral_owasp_v2_flags(name: str, schema: Any) -> bool:
    """spectral-owasp-ruleset 2.0.1 `owasp:api1:2023-no-numeric-ids`, re-expressed.

    given: name === "id" || name.match(/(_id|Id|-id)$/), then: schema must NOT validate
    against {properties: {type: {const: "integer"}}}. That inner schema is satisfied when
    `type` is absent, so an untyped schema is flagged and a ["integer", "null"] array is not.
    """
    if not (name == "id" or re.search(r"(_id|Id|-id)$", name)):
        return False
    if not isinstance(schema, Mapping):
        return False
    body = cast("Mapping[str, Any]", schema)
    return "type" not in body or body["type"] == "integer"


def vacuum_owasp_flags(name: str, schema: Any) -> bool:
    """vacuum 0.39.0 functions/owasp/no_numeric_ids.go `NoNumericIds.RunRule`, re-expressed.

    The name test lower-cases and checks HasSuffix "id" (which subsumes "_id" and "-id"),
    and the type test is slices.Contains(type, "integer") over the normalised type list.
    """
    if not name.lower().endswith("id"):
        return False
    return allows_integer(schema)
