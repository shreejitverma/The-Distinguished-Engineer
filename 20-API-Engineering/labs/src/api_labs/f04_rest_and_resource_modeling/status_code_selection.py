"""HTTP status code selection: success codes, redirect method rewriting, and 4xx precedence."""

import itertools
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Annotated

import h11
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"
REDIRECT_CODES = (301, 302, 303, 307, 308)
# RFC 9110 reason phrases; CPython 3.12 still reports the RFC 7231 / RFC 4918 wording.
RFC9110_PHRASES = {413: "Content Too Large", 422: "Unprocessable Content"}


@dataclass(frozen=True, slots=True)
class RequestFacts:
    """What the server knows about a request when it must choose a 4xx status."""

    well_formed: bool = True
    authenticated: bool = True
    resource_exists: bool = True
    visible_to_caller: bool = True
    permitted: bool = True
    media_type_supported: bool = True
    semantically_valid: bool = True
    conflicts_with_state: bool = False


def select_client_error(facts: RequestFacts) -> int | None:
    """Return the 4xx code of the first failed check, or None when the request may proceed."""
    if not facts.well_formed:
        return 400
    if not facts.authenticated:
        return 401
    if not (facts.resource_exists and facts.visible_to_caller):
        return 404
    if not facts.permitted:
        return 403
    if not facts.media_type_supported:
        return 415
    if not facts.semantically_valid:
        return 422
    if facts.conflicts_with_state:
        return 409
    return None


def select_success_status(*, enacted: bool, created: bool, returns_body: bool) -> int:
    """Pick 202, 201, 200, or 204 for a request that the server accepted."""
    if not enacted:
        return 202
    if created:
        return 201
    return 200 if returns_body else 204


def rfc9110_redirect_method(method: str, status: int) -> str:
    """Method a user agent sends after following a redirect (RFC 9110 Section 15.4)."""
    if status not in REDIRECT_CODES:
        raise ValueError(f"{status} is not an automatic redirect status")
    if status == 303 and method != "HEAD":
        return "GET"
    if status in (301, 302) and method == "POST":
        return "GET"
    return method


def reason_phrase(status: int) -> str:
    """RFC 9110 reason phrase, independent of the running CPython's http.HTTPStatus table."""
    return RFC9110_PHRASES.get(status, HTTPStatus(status).phrase)


def problem(status: int, detail: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    """RFC 9457 problem details whose status member always equals the HTTP status."""
    content = {
        "type": "about:blank",
        "title": reason_phrase(status),
        "status": status,
        "detail": detail,
    }
    return JSONResponse(content, status_code=status, headers=headers, media_type=PROBLEM_JSON)


class WidgetIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=0)


class Widget(WidgetIn):
    id: str
    tenant: str


@dataclass(frozen=True, slots=True)
class Principal:
    tenant: str
    scopes: frozenset[str]


TOKENS: dict[str, Principal] = {
    "alice-rw": Principal("alice", frozenset({"read", "write"})),
    "alice-ro": Principal("alice", frozenset({"read"})),
    "bob-rw": Principal("bob", frozenset({"read", "write"})),
}


@dataclass
class WidgetStore:
    widgets: dict[str, Widget] = field(default_factory=dict[str, Widget])
    ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))


bearer = HTTPBearer(auto_error=False)


def authenticate(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    """401 with a Bearer challenge; RFC 6750 Section 3.1 omits error= when no token was sent."""
    if creds is None:
        challenge = 'Bearer realm="widgets"'
        raise HTTPException(401, "missing bearer token", {"WWW-Authenticate": challenge})
    principal = TOKENS.get(creds.credentials)
    if principal is None:
        challenge = 'Bearer realm="widgets", error="invalid_token"'
        raise HTTPException(401, "invalid bearer token", {"WWW-Authenticate": challenge})
    return principal


def require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        challenge = f'Bearer realm="widgets", error="insufficient_scope", scope="{scope}"'
        raise HTTPException(403, f"token lacks scope {scope}", {"WWW-Authenticate": challenge})


def writer(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
    require_scope(principal, "write")
    return principal


def require_json(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(415, "send application/json")


def build_widget_app(store: WidgetStore | None = None) -> FastAPI:
    """Widget API whose every status code follows the decision tree in select_client_error."""
    db = store if store is not None else WidgetStore()
    app = FastAPI()

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> Response:
        return problem(exc.status_code, str(exc.detail), exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> Response:
        if any(error["type"] == "json_invalid" for error in exc.errors()):
            return problem(400, "request body is not valid JSON")
        return problem(422, "request body failed validation")

    def find_visible(widget_id: str, principal: Principal) -> Widget:
        widget = db.widgets.get(widget_id)
        if widget is None or widget.tenant != principal.tenant:
            raise HTTPException(404, "widget not found")
        return widget

    @app.post("/widgets", status_code=201)
    async def create_widget(
        principal: Annotated[Principal, Depends(writer)],
        _json: Annotated[None, Depends(require_json)],
        body: WidgetIn,
        response: Response,
    ) -> Widget:
        if any(w.tenant == principal.tenant and w.name == body.name for w in db.widgets.values()):
            raise HTTPException(409, f"widget {body.name} already exists")
        widget = Widget(id=f"w{next(db.ids)}", tenant=principal.tenant, **body.model_dump())
        db.widgets[widget.id] = widget
        response.headers["Location"] = f"/widgets/{widget.id}"
        return widget

    @app.get("/widgets/{widget_id}")
    async def read_widget(
        widget_id: str, principal: Annotated[Principal, Depends(authenticate)]
    ) -> Widget:
        return find_visible(widget_id, principal)

    @app.delete("/widgets/{widget_id}", status_code=204, response_class=Response)
    async def delete_widget(
        widget_id: str, principal: Annotated[Principal, Depends(authenticate)]
    ) -> Response:
        find_visible(widget_id, principal)
        require_scope(principal, "write")
        del db.widgets[widget_id]
        return Response(status_code=204)

    @app.post("/exports", status_code=202)
    async def start_export(principal: Annotated[Principal, Depends(writer)]) -> dict[str, str]:
        operation = f"operations/op-{next(db.ids)}"
        return {"name": operation, "state": "RUNNING", "monitor": f"/{operation}"}

    @app.get("/v0/widgets/{widget_id}")
    async def moved_widget(widget_id: str) -> RedirectResponse:
        return RedirectResponse(f"/widgets/{widget_id}", status_code=308)

    return app


ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


def build_redirect_lab_app(hits: list[str] | None = None) -> FastAPI:
    """Redirect to /echo with any status so a client's method rewriting is observable."""
    seen = hits if hits is not None else []
    app = FastAPI()

    @app.api_route("/redirect/{code}", methods=ALL_METHODS)
    async def redirect(code: int, target: str = "/echo") -> RedirectResponse:
        return RedirectResponse(target, status_code=code)

    @app.api_route("/echo", methods=ALL_METHODS)
    async def echo(request: Request) -> dict[str, str | None]:
        body = await request.body()
        return {
            "method": request.method,
            "body": body.decode(),
            "content_type": request.headers.get("content-type"),
            "authorization": request.headers.get("authorization"),
        }

    @app.get("/loop")
    async def loop() -> RedirectResponse:
        seen.append("/loop")
        return RedirectResponse("/loop", status_code=302)

    return app


def h11_server_bytes(status: int, body: bytes) -> bytes:
    """Serialize one response with h11, which enforces RFC 9112 Section 6.3 framing."""
    conn = h11.Connection(our_role=h11.SERVER)
    conn.receive_data(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
    conn.next_event()
    conn.next_event()
    headers = [("Content-Length", str(len(body)))]
    out = conn.send(h11.Response(status_code=status, headers=headers)) or b""
    out += conn.send(h11.Data(data=body)) or b""
    return out + (conn.send(h11.EndOfMessage()) or b"")


def h11_client_read(wire: bytes, requests: int) -> list[tuple[int, bytes]]:
    """Parse `requests` sequential responses from one byte stream as an HTTP/1.1 client."""
    conn = h11.Connection(our_role=h11.CLIENT)
    conn.receive_data(wire)
    results: list[tuple[int, bytes]] = []
    for _ in range(requests):
        if results:
            conn.start_next_cycle()
        conn.send(h11.Request(method="GET", target="/", headers=[("Host", "test")]))
        conn.send(h11.EndOfMessage())
        status, body = 0, b""
        while True:
            event = conn.next_event()
            if isinstance(event, h11.Response):
                status = event.status_code
            elif isinstance(event, h11.Data):
                body += bytes(event.data)
            elif isinstance(event, h11.EndOfMessage):
                break
            else:
                raise AssertionError(f"unexpected h11 event {event!r}")
        results.append((status, body))
    return results
