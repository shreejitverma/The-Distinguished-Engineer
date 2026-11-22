"""FastAPI exception handling: HTTPException, RequestValidationError, ResponseValidationError."""

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketException,
)
from fastapi.exceptions import (
    RequestValidationError,
    ResponseValidationError,
    WebSocketRequestValidationError,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request

# A module-level instance raised by many requests, the case Starlette's traceback reset targets.
SHARED_NOT_FOUND = HTTPException(status_code=404, detail="Item not found")


class ItemIn(BaseModel):
    """Inbound client payload model."""

    id: int
    name: str
    price: float = Field(gt=0.0)


class ItemOut(BaseModel):
    """Outbound server response model requiring strict schema conformance."""

    id: int
    name: str
    price: float
    sku: str


class StreamItem(BaseModel):
    """One JSON Lines record, validated per item by FastAPI's stream serializer."""

    id: int


class Credentials(BaseModel):
    """A body whose model hides inputs in str(ValidationError), which FastAPI does not use."""

    model_config = ConfigDict(hide_input_in_errors=True)

    pin: int


class ProblemDetailResponse(JSONResponse):
    """RFC 9457 Problem Details for HTTP APIs response."""

    media_type = "application/problem+json"


class AuditMiddleware(BaseHTTPMiddleware):
    """Middleware capturing the response status after ExceptionMiddleware processing."""

    def __init__(self, app: Any, log_sink: list[str]) -> None:
        super().__init__(app)
        self.log_sink = log_sink

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            response = await call_next(request)
            self.log_sink.append(f"AUDIT: completed with {response.status_code}")
            return response
        except Exception as exc:
            self.log_sink.append(f"AUDIT: unhandled exception {type(exc).__name__}")
            raise


def build_exception_handling_app(log_sink: list[str] | None = None) -> FastAPI:
    """Build a FastAPI application demonstrating the default exception handling paths."""
    app = FastAPI()

    if log_sink is not None:
        app.add_middleware(AuditMiddleware, log_sink=log_sink)

    @app.get("/http-exception")
    async def get_http_exception() -> None:
        raise HTTPException(
            status_code=400,
            detail="Client requested invalid query parameters",
            headers={"X-Error-Code": "ERR_INVALID_PARAM"},
        )

    @app.get("/no-content-exception")
    async def get_no_content_exception() -> None:
        raise HTTPException(
            status_code=204,
            detail="Should be ignored per RFC 9110 Section 6.4.1",
            headers={"X-Empty": "true"},
        )

    @app.get("/starlette-http-exception")
    async def get_starlette_http_exception() -> None:
        raise StarletteHTTPException(
            status_code=403,
            detail="Starlette forbidden directly raised",
            headers={"X-Source": "Starlette"},
        )

    @app.get("/shared-not-found")
    async def get_shared_not_found() -> None:
        raise SHARED_NOT_FOUND

    @app.post("/items/{item_id}", response_model=ItemOut)
    async def create_item(
        item_id: int,
        item: ItemIn,
        x_tenant: Annotated[str, Header()],
    ) -> dict[str, Any]:
        return {
            "id": item.id,
            "name": f"{x_tenant}:{item.name}",
            "price": item.price,
            "sku": f"SKU-{item_id}-{item.id}",
        }

    @app.post("/credentials")
    async def post_credentials(credentials: Credentials) -> dict[str, bool]:
        return {"ok": credentials.pin > 0}

    @app.get("/server-bug-response", response_model=ItemOut)
    async def get_server_bug_response() -> dict[str, Any]:
        # Server code returns data missing 'price' and 'sku', triggering ResponseValidationError.
        return {"id": 42, "name": "Faulty Product"}

    @app.get("/jsonl-bug")
    async def get_jsonl_bug() -> AsyncIterator[StreamItem]:
        # The second item fails per-item validation after the 200 headers are sent.
        yield StreamItem(id=1)
        yield {"id": "not-an-int"}  # type: ignore[misc]

    @app.get("/streaming-error")
    async def get_streaming_error() -> StreamingResponse:
        async def event_generator() -> AsyncIterator[str]:
            yield "first-chunk-ok\n"
            raise RuntimeError("Failure after stream start")

        return StreamingResponse(event_generator(), media_type="text/plain")

    @app.get("/streaming-http-exception")
    async def get_streaming_http_exception() -> StreamingResponse:
        async def event_generator() -> AsyncIterator[str]:
            yield "first-chunk-ok\n"
            raise HTTPException(status_code=409, detail="Handler exists but headers are sent")

        return StreamingResponse(event_generator(), media_type="text/plain")

    @app.websocket("/ws-validate")
    async def ws_validate_endpoint(
        websocket: WebSocket,
        token: Annotated[int, Query()],
    ) -> None:
        await websocket.accept()
        await websocket.send_text(f"Authenticated with token {token}")
        await websocket.close()

    @app.websocket("/ws-policy")
    async def ws_policy_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        raise WebSocketException(code=1008, reason="Policy violation occurred")

    @app.websocket("/ws-deny")
    async def ws_deny_endpoint(websocket: WebSocket) -> None:
        # Raised before accept: the HTTPException handler's JSONResponse becomes a denial response.
        raise HTTPException(
            status_code=401,
            detail="Missing session token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return app


def build_custom_handler_app(captured_server_errors: list[dict[str, Any]]) -> FastAPI:
    """Build a FastAPI application with custom handlers for RFC 9457 and ResponseValidationError."""
    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def rfc9457_validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # Field paths and messages only: the offending 'input' is never echoed to the caller.
        errors = [
            {
                "pointer": "#/" + "/".join(str(loc) for loc in error.get("loc", ())[1:]),
                "location": str(error.get("loc", ("unknown",))[0]),
                "detail": error.get("msg", ""),
                "code": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        problem_doc = {
            "type": "https://errors.example.com/invalid-request",
            "title": "Invalid Request Content",
            "status": 422,
            "detail": f"Request input validation failed with {len(errors)} error(s).",
            "instance": request.url.path,
            "errors": errors,
        }
        return ProblemDetailResponse(content=problem_doc, status_code=422)

    @app.exception_handler(ResponseValidationError)
    async def custom_response_validation_handler(
        request: Request,
        exc: ResponseValidationError,
    ) -> JSONResponse:
        # Capture internal violation telemetry while redacting internal details from the client.
        captured_server_errors.append(
            {
                "path": request.url.path,
                "body": exc.body,
                "errors": exc.errors(),
            }
        )
        problem_doc = {
            "type": "https://errors.example.com/internal-contract-violation",
            "title": "Internal Schema Contract Violation",
            "status": 500,
            "detail": "An internal error occurred while validating the service response.",
            "instance": request.url.path,
        }
        return ProblemDetailResponse(content=problem_doc, status_code=500)

    @app.exception_handler(WebSocketRequestValidationError)
    async def ws_validation_denial_handler(
        websocket: WebSocket,
        exc: WebSocketRequestValidationError,
    ) -> None:
        # Answer the handshake with a real HTTP response instead of a pre-accept close.
        fields = [".".join(str(loc) for loc in error.get("loc", ())) for error in exc.errors()]
        await websocket.send_denial_response(
            JSONResponse({"invalid": fields}, status_code=422),
        )

    @app.post("/custom-items", response_model=ItemOut)
    async def custom_create_item(item: ItemIn) -> dict[str, Any]:
        return {
            "id": item.id,
            "name": item.name,
            "price": item.price,
            "sku": f"SKU-{item.id}",
        }

    @app.get("/custom-broken", response_model=ItemOut)
    async def custom_broken_endpoint() -> dict[str, Any]:
        return {"id": 99, "name": "Broken Output"}

    @app.websocket("/custom-ws")
    async def custom_ws(websocket: WebSocket, token: Annotated[int, Query()]) -> None:
        await websocket.accept()
        await websocket.send_text(f"token={token}")
        await websocket.close()

    return app


def build_catch_all_app(seen: list[str]) -> FastAPI:
    """Build an app whose Exception handler is moved into ServerErrorMiddleware by FastAPI."""
    app = FastAPI()

    @app.exception_handler(Exception)
    async def catch_all(request: Request, exc: Exception) -> JSONResponse:
        seen.append(type(exc).__name__)
        return JSONResponse({"error": "internal"}, status_code=500)

    @app.exception_handler(404)
    async def not_found_by_status(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": "no such resource"}, status_code=404)

    @app.get("/boom")
    async def boom() -> None:
        raise ValueError("database row decoder failed")

    @app.get("/missing")
    async def missing() -> None:
        raise HTTPException(status_code=404, detail="per-route detail")

    return app


def build_yield_swallow_app(log: list[str]) -> FastAPI:
    """Build an app whose yield dependencies swallow endpoint errors in each scope."""
    app = FastAPI()

    async def swallow_request_scope() -> AsyncIterator[str]:
        try:
            yield "request-scope"
        except Exception as exc:  # the bug under test: no re-raise
            log.append(f"request-scope swallowed {type(exc).__name__}")

    async def swallow_function_scope() -> AsyncIterator[str]:
        try:
            yield "function-scope"
        except Exception as exc:  # the bug under test: no re-raise
            log.append(f"function-scope swallowed {type(exc).__name__}")

    async def observe_and_reraise() -> AsyncIterator[str]:
        try:
            yield "observer"
        except HTTPException as exc:
            log.append(f"observer saw {exc.status_code} before the handler ran")
            raise

    @app.get("/swallow-request")
    async def swallow_request(dep: Annotated[str, Depends(swallow_request_scope)]) -> None:
        raise ValueError(dep)

    @app.get("/swallow-function")
    async def swallow_function(
        dep: Annotated[str, Depends(swallow_function_scope, scope="function")],
    ) -> None:
        raise ValueError(dep)

    @app.get("/observe")
    async def observe(dep: Annotated[str, Depends(observe_and_reraise)]) -> None:
        raise HTTPException(status_code=409, detail=dep)

    return app


def build_starlette_mro_demo_app() -> tuple[FastAPI, FastAPI]:
    """Build two apps demonstrating Starlette vs FastAPI HTTPException MRO handler lookup."""
    app_fastapi_only = FastAPI()
    app_starlette_base = FastAPI()

    @app_fastapi_only.exception_handler(HTTPException)
    async def custom_fastapi_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"custom_handler": "fastapi_http_exception", "detail": exc.detail},
        )

    @app_starlette_base.exception_handler(StarletteHTTPException)
    async def custom_starlette_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"custom_handler": "starlette_http_exception", "detail": exc.detail},
        )

    return app_fastapi_only, app_starlette_base
