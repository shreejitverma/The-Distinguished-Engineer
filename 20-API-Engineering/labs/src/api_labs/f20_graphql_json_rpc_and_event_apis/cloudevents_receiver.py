"""A minimal ASGI webhook receiver for CloudEvents over HTTP (delivery plus abuse protection)."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from api_labs.f20_graphql_json_rpc_and_event_apis.cloudevents_envelope import (
    CloudEventError,
    UnsupportedEventFormatError,
    parse_message,
)


def build_app(*, allowed_origins: frozenset[str], max_body: int = 65536) -> Starlette:
    """`POST /events` delivers; `OPTIONS /events` runs the http-webhook.md validation handshake."""

    async def deliver(request: Request) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_body:
                return Response(status_code=413)
        try:
            headers = [(k.decode("ascii"), v.decode("ascii")) for k, v in request.headers.raw]
            parsed = parse_message(headers, bytes(body))
        except UnsupportedEventFormatError:
            return Response(status_code=415)
        except (CloudEventError, UnicodeDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {"mode": parsed.mode.value, "ids": [e.id for e in parsed.events]}, status_code=202
        )

    async def validate(request: Request) -> Response:
        origin = request.headers.get("webhook-request-origin")
        if origin is None or origin not in allowed_origins:
            return Response(status_code=405, headers={"Allow": "POST"})
        headers = {"Allow": "POST", "WebHook-Allowed-Origin": origin}
        requested = request.headers.get("webhook-request-rate")
        headers["WebHook-Allowed-Rate"] = requested if requested is not None else "*"
        return Response(status_code=200, headers=headers)

    return Starlette(
        routes=[
            Route("/events", deliver, methods=["POST"]),
            Route("/events", validate, methods=["OPTIONS"]),
        ]
    )
