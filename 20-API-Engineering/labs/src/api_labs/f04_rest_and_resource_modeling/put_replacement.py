"""Full-replacement PUT, omitted-field defaulting, and client-supplied ID creation.

RFC 9110 Section 9.3.4 defines PUT as "created or replaced with the state defined by the
representation enclosed"; Google AIP-134 warns that adding a field then breaks old clients, and
AIP-122 and AIP-133 govern user-specified IDs. Everything here is in-process and deterministic.
"""

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Final, Self

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

# AIP-122: lowercase RFC 1034 label, first a letter, last a letter or digit, at most 63 characters.
RESOURCE_ID: Final = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
# RFC 9110 Section 8.8.3: entity-tag = [ "W/" ] DQUOTE *etagc DQUOTE,
# etagc = %x21 / %x23-7E / obs-text, obs-text = %x80-FF (Starlette decodes fields as Latin-1).
_ENTITY_TAG: Final = re.compile(r'(W/)?"[\x21\x23-\x7e\x80-\xff]*"')
_LIST_SEPARATOR: Final = re.compile(r"[ \t]*,[ \t]*")

MUTABLE_FIELDS: Final = frozenset({"display_name", "color", "labels"})


class WidgetBody(BaseModel):
    """The PUT body the server accepts after `labels` was added to the resource (schema v2)."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=63)
    color: str = "black"
    labels: dict[str, str] = Field(default_factory=dict[str, str])
    # Output-only members (AIP-203): accepted so a GET body can be PUT back, then discarded.
    id: str | None = None
    revision: int | None = None
    etag: str | None = None


class WidgetV1(BaseModel):
    """What a client generated before `labels` existed knows; `extra` defaults to `'ignore'`."""

    display_name: str
    color: str = "black"
    id: str | None = None
    revision: int | None = None
    etag: str | None = None


class LosslessWidgetV1(WidgetV1):
    """The same old client, but it keeps members it does not understand and sends them back."""

    model_config = ConfigDict(extra="allow")


class OmittedFields(StrEnum):
    """What the server does with a mutable field the PUT body leaves out."""

    DEFAULT = "default"  # full replacement: the schema default wins (RFC 9110 reading)
    REJECT = "reject"  # every mutable field must be present, else 422
    PRESERVE = "preserve"  # keep the stored value; PUT degrades into a merge for omissions


@dataclass(frozen=True, slots=True)
class StoredWidget:
    id: str
    display_name: str
    color: str
    labels: dict[str, str]
    revision: int

    @property
    def etag(self) -> str:
        return f'"r{self.revision}"'

    def same_state(self, other: Self) -> bool:
        return (self.display_name, self.color, self.labels) == (
            other.display_name,
            other.color,
            other.labels,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "color": self.color,
            "labels": dict(self.labels),
            "revision": self.revision,
            "etag": self.etag,
        }


def parse_entity_tags(value: str) -> list[str] | None:
    """Split an `If-Match` or `If-None-Match` list; `None` means syntactically invalid."""
    tags: list[str] = []
    for element in _LIST_SEPARATOR.split(value.strip()):
        if not element:
            continue  # RFC 9110 Section 5.6.1.2: recipients accept empty list elements
        if not _ENTITY_TAG.fullmatch(element):
            return None
        tags.append(element)
    return tags


def preconditions_hold(
    if_match: str | None, if_none_match: str | None, current: StoredWidget | None
) -> bool | None:
    """RFC 9110 Section 13.2.2 steps 1 and 3 for an unsafe method; `None` means malformed.

    Step 2 (`If-Unmodified-Since`) is out of scope: this resource has no `Last-Modified`.
    """
    if if_match is not None:
        if if_match.strip() == "*":
            matched = current is not None
        else:
            tags = parse_entity_tags(if_match)
            if tags is None:
                return None
            # Strong comparison (RFC 9110 Section 13.1.1): a weak tag never matches.
            matched = current is not None and current.etag in tags
        if not matched:
            return False
        # A true If-Match continues to step 3, so If-None-Match is still evaluated.
    if if_none_match is not None:
        if if_none_match.strip() == "*":
            return current is None
        tags = parse_entity_tags(if_none_match)
        if tags is None:
            return None
        # Weak comparison (RFC 9110 Section 13.1.2): ignore the W/ prefix.
        return current is None or current.etag not in {t.removeprefix("W/") for t in tags}
    return True


def _field(request: Request, name: str) -> str | None:
    lines = request.headers.getlist(name)
    return ", ".join(lines) if lines else None


def _problem(status: int, detail: object) -> JSONResponse:
    return JSONResponse({"status": status, "detail": detail}, status_code=status)


@dataclass
class WidgetService:
    """One collection, `/widgets/{id}`, whose IDs the client chooses."""

    omitted: OmittedFields = OmittedFields.DEFAULT
    allow_missing: bool = True
    widgets: dict[str, StoredWidget] = field(default_factory=dict[str, StoredWidget])
    writes: int = 0

    async def get(self, request: Request) -> Response:
        current = self.widgets.get(str(request.path_params["widget_id"]))
        if current is None:
            return _problem(404, "no such widget")
        return JSONResponse(current.to_json(), headers={"etag": current.etag})

    async def put(self, request: Request) -> Response:
        widget_id = str(request.path_params["widget_id"])
        # Normal request checks first; RFC 9110 Section 13.2.1 says a 4xx here beats any 412.
        if not RESOURCE_ID.fullmatch(widget_id):
            return _problem(400, "widget id must match ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
        if "content-range" in request.headers:
            return _problem(400, "partial PUT is not supported")  # RFC 9110 Section 14.5
        current = self.widgets.get(widget_id)
        if current is None and not self.allow_missing:
            return _problem(404, "no such widget and allow_missing is false")

        # Preconditions are evaluated before the content is processed (RFC 9110 Section 13.2.1).
        verdict = preconditions_hold(
            _field(request, "if-match"), _field(request, "if-none-match"), current
        )
        if verdict is None:
            return _problem(400, "malformed entity-tag list")
        if not verdict:
            return _problem(412, "precondition failed")

        try:
            raw: object = json.loads(await request.body())
        except ValueError:
            return _problem(400, "body is not JSON")
        if not isinstance(raw, dict):
            return _problem(400, "body must be a JSON object")
        try:
            body = WidgetBody.model_validate(raw)
        except ValidationError as exc:
            return _problem(422, exc.errors(include_url=False, include_context=False))
        if body.id is not None and body.id != widget_id:
            return _problem(409, f"body id {body.id!r} does not match the target URI")

        missing = MUTABLE_FIELDS - body.model_fields_set
        if self.omitted is OmittedFields.REJECT and missing:
            return _problem(422, {"missing": sorted(missing)})
        keep = current if self.omitted is OmittedFields.PRESERVE else None
        candidate = StoredWidget(
            id=widget_id,
            display_name=body.display_name,
            color=keep.color if keep and "color" in missing else body.color,
            labels=keep.labels if keep and "labels" in missing else dict(body.labels),
            revision=1 if current is None else current.revision + 1,
        )
        if current is not None and current.same_state(candidate):
            return JSONResponse(current.to_json(), status_code=200)  # no-op: revision unchanged

        self.widgets[widget_id] = candidate
        self.writes += 1
        # 201 on create, 200 on replace (RFC 9110 Section 9.3.4). No ETag header: the stored
        # representation is not the bytes received, so a validator field is forbidden.
        return JSONResponse(candidate.to_json(), status_code=201 if current is None else 200)


def build_app(service: WidgetService) -> Starlette:
    return Starlette(
        routes=[
            Route("/widgets/{widget_id}", service.get, methods=["GET"]),
            Route("/widgets/{widget_id}", service.put, methods=["PUT"]),
        ]
    )


def build_typed_body_app(service: WidgetService) -> FastAPI:
    """The FastAPI tutorial shape: the body is a typed parameter, checked before the handler."""
    app = FastAPI()

    @app.put("/widgets/{widget_id}")
    async def put_widget(  # pyright: ignore[reportUnusedFunction]
        widget_id: str,
        body: WidgetBody,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        current = service.widgets.get(widget_id)
        if if_match is not None and (current is None or current.etag != if_match):
            raise HTTPException(status_code=412, detail="precondition failed")
        stored = StoredWidget(
            id=widget_id,
            display_name=body.display_name,
            color=body.color,
            labels=dict(body.labels),
            revision=1 if current is None else current.revision + 1,
        )
        service.widgets[widget_id] = stored
        service.writes += 1
        return stored.to_json()

    return app
