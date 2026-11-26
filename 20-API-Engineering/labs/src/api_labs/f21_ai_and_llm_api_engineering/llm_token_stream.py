"""A streaming LLM token API over SSE: delta frames, heartbeats, and errors after `200 OK`.

Three decisions, each isolated so a test can break it:

* Framing: every frame is `event: <name>` plus one `data:` line of compact JSON, so token text
  with newlines or `U+2028` can never split a frame (`encode_frame`).
* Liveness: a `TokenPump` task owns the upstream generator and feeds a one-slot queue, so the
  heartbeat timer waits on the queue and never cancels the generator (`frame_stream`).
* Failure: before the first item the handler can still answer with a real status; after it, the
  status is committed and failure is a terminal `error` frame carrying an RFC 9457 object.

`consume` is the client half: the end of the stream is judged by the terminal frame, never by
the connection closing.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from api_labs.f10_asgi_and_starlette_internals.sse_resumption import EventStreamParser

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from starlette.requests import Request
    from starlette.responses import Response

HEARTBEAT_INTERVAL_S: Final = 15.0  # HTML 9.2.7: "every 15 seconds or so"
HEARTBEAT_FRAME: Final = b": ping\n\n"
PROBLEM_JSON: Final = "application/problem+json"
PROBLEM_BASE: Final = "https://api.example.com/problems/"
SSE_HEADERS: Final = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
MAX_REQUEST_ID_CHARS: Final = 64

_EVENT_NAME = re.compile(r"[a-z][a-z_]*")

Generate = Callable[[str], "AsyncGenerator[str, None]"]


# --- Framing -----------------------------------------------------------------------------


def encode_frame(event: str, data: Mapping[str, object]) -> bytes:
    """One SSE frame: `event:` line, one `data:` line of compact JSON, blank line.

    JSON escapes CR and LF inside strings, so the payload is always a single line.
    """
    if _EVENT_NAME.fullmatch(event) is None:
        raise ValueError(f"invalid SSE event name: {event!r}")
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


@dataclass(frozen=True, slots=True)
class Problem:
    """An RFC 9457 problem details object, plus one extension member (`retry_after`)."""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    retry_after: int | None = None

    def to_json(self) -> dict[str, object]:
        body: dict[str, object] = {
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "instance": self.instance,
        }
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body


class GenerationError(Exception):
    """A failure whose description is safe to show the client."""

    def __init__(
        self, slug: str, title: str, status: int, detail: str, retry_after: int | None = None
    ) -> None:
        super().__init__(f"{slug}: {detail}")
        self.slug = slug
        self.title = title
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


def problem_for(exc: Exception, request_id: str) -> Problem:
    """Map an exception to a problem; only `GenerationError` text ever reaches the client."""
    instance = "/v1/requests/" + quote(request_id, safe="")
    if isinstance(exc, GenerationError):
        return Problem(
            PROBLEM_BASE + exc.slug, exc.title, exc.status, exc.detail, instance, exc.retry_after
        )
    return Problem(
        PROBLEM_BASE + "internal-error",
        "Generation failed",
        500,
        "The server failed while generating; quote the instance when reporting it.",
        instance,
    )


# --- Producer side -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Token:
    text: str


@dataclass(frozen=True, slots=True)
class End:
    pass


@dataclass(frozen=True, slots=True)
class Failed:
    error: Exception


Item = Token | End | Failed


class TokenPump:
    """Owns the upstream generator in its own task and hands items over a one-slot queue.

    The consumer can time out on `get` without touching the generator, which is the whole
    point: `asyncio.wait_for(gen.__anext__(), t)` would cancel the generator instead.
    """

    def __init__(self, tokens: AsyncGenerator[str, None]) -> None:
        self._tokens = tokens
        self._queue: asyncio.Queue[Item] = asyncio.Queue(maxsize=1)
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            async for text in self._tokens:
                await self._queue.put(Token(text))
            await self._queue.put(End())
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # the consumer cancelled us (`cancel` or `aclose`): honour it
            # The upstream raised CancelledError on its own; without this the consumer's `get`
            # would wait forever, so report it as a failure like any other exception.
            await self._queue.put(Failed(RuntimeError("upstream generator was cancelled")))
        except Exception as exc:
            await self._queue.put(Failed(exc))
        finally:
            await self._tokens.aclose()

    @property
    def done(self) -> bool:
        return self._task.done()

    async def get(self, idle_s: float | None = None) -> Item | None:
        """The next item, or `None` if nothing arrived within `idle_s` seconds."""
        try:
            return await asyncio.wait_for(self._queue.get(), idle_s)
        except TimeoutError:
            return None

    def cancel(self) -> None:
        self._task.cancel()

    async def aclose(self) -> None:
        """Cancel the producer and wait for its cleanup; never swallows our own cancellation."""
        self._task.cancel()
        await asyncio.wait({self._task})
        if not self._task.cancelled():
            self._task.result()


async def frame_stream(
    pump: TokenPump,
    *,
    request_id: str,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
    head: Item | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield wire frames: `delta`* with `: ping` while idle, then exactly one terminal frame."""
    seq = 0
    pending = head
    try:
        while True:
            item = pending if pending is not None else await pump.get(heartbeat_interval)
            pending = None
            if item is None:
                yield HEARTBEAT_FRAME
            elif isinstance(item, Token):
                try:
                    frame = encode_frame("delta", {"seq": seq, "text": item.text})
                except UnicodeEncodeError as exc:  # a lone surrogate from a bad detokenizer
                    yield encode_frame("error", problem_for(exc, request_id).to_json())
                    return
                yield frame
                seq += 1
            elif isinstance(item, End):
                yield encode_frame("done", {"finish_reason": "stop", "deltas": seq})
                return
            else:
                yield encode_frame("error", problem_for(item.error, request_id).to_json())
                return
    finally:
        await pump.aclose()


async def naive_heartbeat_frames(
    tokens: AsyncGenerator[str, None], interval: float
) -> AsyncGenerator[bytes, None]:
    """The anti-pattern: `wait_for` on `__anext__` cancels the generator at the first timeout."""
    iterator = tokens.__aiter__()
    seq = 0
    while True:
        try:
            text = await asyncio.wait_for(iterator.__anext__(), interval)
        except TimeoutError:
            yield HEARTBEAT_FRAME
            continue
        except StopAsyncIteration:
            return
        yield encode_frame("delta", {"seq": seq, "text": text})
        seq += 1


# --- HTTP surface ------------------------------------------------------------------------


def problem_response(problem: Problem) -> JSONResponse:
    headers = {"Retry-After": str(problem.retry_after)} if problem.retry_after is not None else None
    return JSONResponse(
        problem.to_json(), status_code=problem.status, media_type=PROBLEM_JSON, headers=headers
    )


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id")
    return (supplied or uuid.uuid4().hex)[:MAX_REQUEST_ID_CHARS]


async def _read_prompt(request: Request) -> str | None:
    try:
        body: object = await request.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        prompt = cast("dict[str, object]", body).get("prompt")
        if isinstance(prompt, str):
            return prompt
    return None


def create_app(
    generate: Generate,
    *,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
    commit_after_first_item: bool = True,
) -> Starlette:
    """`POST /v1/completions` streams; `POST /v1/naive` is the same without error handling."""

    async def completions(request: Request) -> Response:
        request_id = _request_id(request)
        prompt = await _read_prompt(request)
        if prompt is None:
            return problem_response(
                Problem(
                    PROBLEM_BASE + "invalid-request",
                    "Invalid request",
                    422,
                    'The body must be a JSON object with a string member "prompt".',
                    "/v1/requests/" + quote(request_id, safe=""),
                )
            )
        pump = TokenPump(generate(prompt))
        head: Item | None = None
        if commit_after_first_item:
            try:
                head = await pump.get()
            except BaseException:
                pump.cancel()
                raise
            if isinstance(head, Failed):
                await pump.aclose()
                return problem_response(problem_for(head.error, request_id))
        return StreamingResponse(
            frame_stream(
                pump, request_id=request_id, heartbeat_interval=heartbeat_interval, head=head
            ),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    async def naive(request: Request) -> Response:
        prompt = await _read_prompt(request)
        if prompt is None:
            return JSONResponse({"error": "bad request"}, status_code=422)

        async def frames() -> AsyncGenerator[bytes, None]:
            seq = 0
            async for text in generate(prompt):
                yield encode_frame("delta", {"seq": seq, "text": text})
                seq += 1

        return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)

    return Starlette(
        routes=[
            Route("/v1/completions", completions, methods=["POST"]),
            Route("/v1/naive", naive, methods=["POST"]),
        ]
    )


# --- Client side -------------------------------------------------------------------------


class Outcome(Enum):
    COMPLETE = "complete"  # a `done` frame arrived
    FAILED = "failed"  # an `error` frame arrived
    TRUNCATED = "truncated"  # the stream ended with no terminal frame


@dataclass(frozen=True, slots=True)
class StreamResult:
    outcome: Outcome
    text: str
    events: tuple[str, ...]
    problem: dict[str, Any] | None
    heartbeats: int


def consume(wire: bytes) -> StreamResult:
    """Judge a finished body by its terminal frame; a frame cut before its blank line is lost."""
    parser = EventStreamParser()
    parsed = parser.feed(wire) + parser.close()
    heartbeats = sum(1 for line in wire.split(b"\n") if line.startswith(b":"))
    text: list[str] = []
    names: list[str] = []
    outcome = Outcome.TRUNCATED
    problem: dict[str, Any] | None = None
    for message in parsed:
        names.append(message.type)
        payload: dict[str, Any] = json.loads(message.data)
        if message.type == "delta":
            text.append(str(payload["text"]))
        elif message.type == "done":
            outcome = Outcome.COMPLETE
            break
        elif message.type == "error":
            outcome, problem = Outcome.FAILED, payload
            break
    return StreamResult(outcome, "".join(text), tuple(names), problem, heartbeats)


def decode_chunked(body: bytes) -> tuple[list[bytes], bool]:
    """Split an HTTP/1.1 chunked body (RFC 9112 Section 7.1); `False` means no last-chunk."""
    chunks: list[bytes] = []
    pos = 0
    while True:
        eol = body.find(b"\r\n", pos)
        if eol < 0:
            return chunks, False
        size = int(body[pos:eol].split(b";", 1)[0], 16)
        if size == 0:
            return chunks, body[eol:].startswith(b"\r\n\r\n")
        start = eol + 2
        end = start + size
        if body[end : end + 2] != b"\r\n":
            return chunks, False
        chunks.append(body[start:end])
        pos = end + 2
