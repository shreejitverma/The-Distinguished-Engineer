"""HTTP content-coding negotiation, codecs, bomb guards, and the BREACH oracle.

Covers RFC 9110 Section 8.4 (Content-Encoding), Section 12.5.3 (Accept-Encoding
q-value negotiation), RFC 1952 (gzip framing), RFC 7932 (brotli), and RFC 8878
with RFC 9659 (zstd window limits).  Every function here is deterministic, runs
in process, and opens no sockets.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import brotli  # type: ignore[import-untyped]
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

# The stdlib zstd binding (PEP 784) exists only on CPython 3.14+.  Import it
# optionally so gzip, brotli, and negotiation still work on 3.12 and 3.13, and
# gate the zstd codec paths on availability.
_zstd: ModuleType | None
try:
    from compression import zstd as _zstd
except ModuleNotFoundError:  # pragma: no cover - exercised only on < 3.14
    _zstd = None
HAS_STDLIB_ZSTD = _zstd is not None

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from types import ModuleType

    from starlette.requests import Request

__all__ = [
    "CODING_MAGIC",
    "HAS_STDLIB_ZSTD",
    "ZSTD_MAX_WINDOW_BYTES",
    "ZSTD_MAX_WINDOW_LOG",
    "CodecMeasurement",
    "DecompressionLimitExceeded",
    "GzipStreamCompressor",
    "TruncatedStreamError",
    "breach_distinguisher",
    "build_demo_app",
    "compress",
    "decompress",
    "decompress_bounded",
    "demo_json_payload",
    "detect_coding",
    "mask_secret",
    "measure_codecs",
    "negotiate_content_coding",
    "reflected_page",
    "server_codings",
]


def server_codings() -> tuple[str, ...]:
    """Codings the demo server offers, most-preferred first (identity implicit).

    zstd is advertised only where the stdlib binding exists (CPython 3.14+), so
    the server never negotiates a coding it cannot produce.
    """
    return ("zstd", "br", "gzip") if HAS_STDLIB_ZSTD else ("br", "gzip")


# Magic-number prefixes that identify a coded stream on the wire.
# gzip: ID1=0x1f ID2=0x8b CM=0x08 (RFC 1952 Section 2.3.1).
# zstd: frame magic 0xFD2FB528 little-endian (RFC 8878 Section 3.1.1).
# brotli has no magic number (RFC 7932 Section 9.1), so it is absent here.
CODING_MAGIC: dict[str, bytes] = {
    "gzip": b"\x1f\x8b\x08",
    "zstd": b"\x28\xb5\x2f\xfd",
}

# zstd window ceiling for the "zstd" content coding: RFC 9659 Section 3 says
# decoders MUST support and encoders MUST NOT exceed 8 MB, so the bounded
# decoder refuses any frame whose window needs more (window_log_max = 23).
ZSTD_MAX_WINDOW_LOG = 23
ZSTD_MAX_WINDOW_BYTES = 1 << ZSTD_MAX_WINDOW_LOG


class _ZstdParameters(Protocol):
    window_log_max: object


class _ZstdModule(Protocol):
    """The subset of ``compression.zstd`` this module uses (optional on < 3.14)."""

    DecompressionParameter: _ZstdParameters

    def compress(self, data: bytes, *, level: int = ...) -> bytes: ...
    def decompress(self, data: bytes) -> bytes: ...
    def ZstdDecompressor(self, *, options: dict[object, int]) -> _ZstdDecompressor: ...


class _ZstdDecompressor(Protocol):
    @property
    def eof(self) -> bool: ...
    @property
    def unused_data(self) -> bytes: ...

    def decompress(self, data: bytes, max_length: int = ...) -> bytes: ...


def _require_zstd() -> _ZstdModule:
    if _zstd is None:
        raise RuntimeError(
            "zstd content coding requires the stdlib compression.zstd module (CPython 3.14+)"
        )
    return cast("_ZstdModule", _zstd)


class _BrotliDecoder(Protocol):
    """The subset of brotli.Decompressor this module calls (brotli ships no stubs)."""

    def process(self, data: bytes, output_buffer_limit: int = ...) -> bytes: ...
    def is_finished(self) -> bool: ...
    def can_accept_more_data(self) -> bool: ...


def _brotli_compress(data: bytes, quality: int) -> bytes:
    return cast("bytes", brotli.compress(data, quality=quality))  # pyright: ignore[reportUnknownMemberType]


def _brotli_decompress(data: bytes) -> bytes:
    return cast("bytes", brotli.decompress(data))  # pyright: ignore[reportUnknownMemberType]


def _brotli_decoder() -> _BrotliDecoder:
    return cast("_BrotliDecoder", brotli.Decompressor())  # pyright: ignore[reportUnknownMemberType]


class DecompressionLimitExceeded(ValueError):
    """Raised when a decoded stream would exceed the caller's output budget."""


class TruncatedStreamError(ValueError):
    """Raised when a coded body ends before its final block, member, or frame."""


# RFC 9110 Section 12.4.2: the "q" parameter name is case-insensitive.
_WEIGHT_RE = re.compile(
    r"^\s*(?P<name>[^;\s]+)\s*(?:;\s*q=(?P<q>[0-9.]+))?\s*$", re.ASCII | re.IGNORECASE
)


def _parse_accept_encoding(value: str) -> dict[str, float]:
    """Parse an Accept-Encoding field into a {coding: qvalue} map (RFC 9110 12.4.2)."""
    prefs: dict[str, float] = {}
    for element in value.split(","):
        match = _WEIGHT_RE.match(element)
        if match is None:
            continue
        name = match["name"].lower()
        raw_q = match["q"]
        try:
            q = 1.0 if raw_q is None else float(raw_q)
        except ValueError:
            continue
        # RFC 9110 Section 12.4.2: qvalue is clamped to the range 0 through 1.
        prefs[name] = min(max(q, 0.0), 1.0)
    return prefs


def negotiate_content_coding(
    accept_encoding: str | None,
    available: Sequence[str],
) -> str | None:
    """Select a content coding for a response per RFC 9110 Section 12.5.3.

    *available* lists the server's codings most-preferred first, excluding
    ``identity`` (always implicitly available).  Returns the chosen coding, or
    ``"identity"`` when the client wants no coding, or ``None`` when the client
    forbade every option including identity (caller answers ``406``, RFC 9110
    Section 15.5.7, or sends identity anyway).
    """
    # Rule 1: no field present means any coding is acceptable; take our first.
    if accept_encoding is None:
        return available[0] if available else "identity"

    prefs = _parse_accept_encoding(accept_encoding)

    # An empty field value means the user agent wants no content coding at all.
    if not prefs:
        return "identity"

    star = prefs.get("*")

    def acceptable(coding: str) -> float | None:
        """Return the effective qvalue for *coding*, or None if unacceptable."""
        if coding in prefs:
            q = prefs[coding]
        elif star is not None:
            q = star
        else:
            return None
        return None if q == 0.0 else q

    # Rank available codings by qvalue, breaking ties by server preference order.
    best: str | None = None
    best_q = 0.0
    for coding in available:
        q = acceptable(coding)
        if q is None:
            continue
        if best is None or q > best_q:
            best, best_q = coding, q

    if best is not None:
        return best

    # No non-identity coding is acceptable.  RFC 9110 Section 12.5.3 Rule 2:
    # identity (no coding) is acceptable by default unless excluded by
    # "identity;q=0", or by "*;q=0" with no more-specific entry for identity.
    if "identity" in prefs:
        identity_ok = prefs["identity"] != 0.0
    elif star is not None:
        identity_ok = star != 0.0
    else:
        identity_ok = True
    return "identity" if identity_ok else None


def compress(coding: str, data: bytes, *, level: int | None = None) -> bytes:
    """Compress *data* with *coding* (``gzip``, ``br``, ``zstd``, or ``identity``)."""
    if coding == "identity":
        return data
    if coding == "gzip":
        return gzip.compress(data, compresslevel=9 if level is None else level, mtime=0)
    if coding == "br":
        return _brotli_compress(data, 11 if level is None else level)
    if coding == "zstd":
        return _require_zstd().compress(data, level=3 if level is None else level)
    raise ValueError(f"unsupported content coding: {coding!r}")


def decompress(coding: str, data: bytes) -> bytes:
    """Decompress *data* that was coded with *coding* (no output cap)."""
    if coding == "identity":
        return data
    if coding == "gzip":
        return gzip.decompress(data)
    if coding == "br":
        return _brotli_decompress(data)
    if coding == "zstd":
        return _require_zstd().decompress(data)
    raise ValueError(f"unsupported content coding: {coding!r}")


def detect_coding(data: bytes) -> str | None:
    """Guess the content coding of *data* from its leading magic bytes.

    Returns ``"gzip"`` or ``"zstd"``; brotli carries no magic number (RFC 7932
    Section 9.1) so a brotli stream returns ``None`` here, which is exactly why
    Content-Encoding is mandatory metadata rather than something a receiver can
    sniff.
    """
    for coding, magic in CODING_MAGIC.items():
        if data.startswith(magic):
            return coding
    return None


def decompress_bounded(coding: str, data: bytes, *, max_output: int) -> bytes:
    """Decompress with a hard output cap, raising past *max_output* bytes.

    This is the guard missing from the streaming paths behind CVE-2025-66471
    (urllib3) and CVE-2025-69223 (aiohttp): a small, highly compressible input
    must never be allowed to allocate unbounded output.  Every decoder call gets
    an output limit of the remaining budget plus one byte, so memory stays
    O(max_output).  A body that ends before its final gzip member, brotli
    meta-block, or zstd frame raises :class:`TruncatedStreamError` instead of
    returning partial output, and a zstd frame whose window exceeds the 8 MB
    HTTP ceiling (RFC 9659 Section 3) is rejected before decoding.
    """
    if max_output < 0:
        raise ValueError("max_output must be non-negative")
    if coding == "identity":
        if len(data) > max_output:
            raise DecompressionLimitExceeded(f"{len(data)} > {max_output}")
        return data
    if coding == "gzip":
        return _decompress_bounded_gzip(data, max_output)
    if coding == "br":
        return _decompress_bounded_brotli(data, max_output)
    if coding == "zstd":
        return _decompress_bounded_zstd(data, max_output)
    raise ValueError(f"unsupported content coding: {coding!r}")


def _check_budget(out: bytearray, max_output: int) -> None:
    if len(out) > max_output:
        raise DecompressionLimitExceeded(f"exceeded {max_output} bytes")


def _decompress_bounded_gzip(data: bytes, max_output: int) -> bytes:
    # A gzip body is a series of members (RFC 1952 Section 2.2); decode each one.
    # max_length is never 0 here (0 would mean "unlimited" to zlib): the budget
    # check raises before the remaining budget can reach zero.
    out = bytearray()
    remaining = data
    while True:
        decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
        out += decoder.decompress(remaining, max_output + 1 - len(out))
        _check_budget(out, max_output)
        if not decoder.eof:
            raise TruncatedStreamError("gzip member ends before its trailer")
        remaining = decoder.unused_data
        if not remaining:
            return bytes(out)


def _decompress_bounded_zstd(data: bytes, max_output: int) -> bytes:
    # A zstd body may hold several frames (RFC 8878 Section 3.1); decode each one,
    # refusing any frame whose window exceeds RFC 9659's 8 MB HTTP ceiling.
    zstd = _require_zstd()
    options = {zstd.DecompressionParameter.window_log_max: ZSTD_MAX_WINDOW_LOG}
    out = bytearray()
    remaining = data
    while True:
        decoder = zstd.ZstdDecompressor(options=options)
        out += decoder.decompress(remaining, max_output + 1 - len(out))
        _check_budget(out, max_output)
        if not decoder.eof:
            raise TruncatedStreamError("zstd frame ends before its last block")
        remaining = decoder.unused_data
        if not remaining:
            return bytes(out)


def _decompress_bounded_brotli(data: bytes, max_output: int) -> bytes:
    decoder = _brotli_decoder()
    out = bytearray(decoder.process(data, output_buffer_limit=max_output + 1))
    # can_accept_more_data() is False while the decoder still holds output.
    while (
        len(out) <= max_output and not decoder.is_finished() and not decoder.can_accept_more_data()
    ):
        out += decoder.process(b"", output_buffer_limit=max_output + 1 - len(out))
    _check_budget(out, max_output)
    if not decoder.is_finished():
        raise TruncatedStreamError("brotli stream ends before its last meta-block")
    return bytes(out)


@dataclass(slots=True)
class GzipStreamCompressor:
    """Incremental gzip compressor mirroring Starlette's GZipResponder.

    Each :meth:`compress` call emits bytes and a ``Z_SYNC_FLUSH`` boundary so a
    streaming response stays flushable to the client (starlette 1.10.0
    starlette/middleware/gzip.py, GZipResponder._compress_body); :meth:`finish`
    writes the gzip trailer.  This is why a streaming compressed response drops
    Content-Length: the final size is unknown until the stream ends.
    """

    level: int = 9
    _compressor: zlib._Compress | None = None  # pyright: ignore[reportPrivateUsage]

    @property
    def _obj(self) -> zlib._Compress:  # pyright: ignore[reportPrivateUsage]
        if self._compressor is None:
            self._compressor = zlib.compressobj(self.level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        return self._compressor

    def compress(self, body: bytes) -> bytes:
        """Compress one chunk and flush it to a syncable boundary."""
        return self._obj.compress(body) + self._obj.flush(zlib.Z_SYNC_FLUSH)

    def finish(self) -> bytes:
        """Flush the final block and gzip trailer (CRC-32 and ISIZE)."""
        return self._obj.flush()


def demo_json_payload(rows: int = 2000) -> bytes:
    """Return a deterministic, deliberately repetitive JSON body for codec comparisons."""
    items = [
        {"id": i, "name": f"user-{i}", "status": "active", "tags": ["api", "http"]}
        for i in range(rows)
    ]
    return json.dumps({"rows": items}, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True, slots=True)
class CodecMeasurement:
    """One codec setting's encoded size and best-of-N encode time."""

    coding: str
    level: int
    size: int
    best_seconds: float


def measure_codecs(payload: bytes, *, repeats: int = 5) -> list[CodecMeasurement]:
    """Encode *payload* with each codec setting; report size and best encode time.

    Sizes are deterministic for a given library build; times are wall-clock
    (``time.perf_counter``) and vary by machine, so only their order transfers.
    Every result is decoded and checked against *payload* before it is reported.
    """
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    settings = [("gzip", 9), ("br", 11)]
    if HAS_STDLIB_ZSTD:
        settings += [("zstd", 3), ("zstd", 19)]
    results: list[CodecMeasurement] = []
    for coding, level in settings:
        best = float("inf")
        blob = b""
        for _ in range(repeats):
            start = time.perf_counter()
            blob = compress(coding, payload, level=level)
            best = min(best, time.perf_counter() - start)
        if decompress(coding, blob) != payload:
            raise AssertionError(f"{coding}-{level} did not round-trip")
        results.append(CodecMeasurement(coding, level, len(blob), best))
    return results


def reflected_page(reflected: str, secret: str) -> bytes:
    """Build a response body that reflects attacker input next to a secret.

    This is the BREACH precondition (Prado, Harris, Gluck, Black Hat USA 2013):
    one response contains both attacker-controlled text and a same-origin secret
    such as a CSRF token.
    """
    return (
        b'<input type="hidden" name="csrf_token" value="'
        + secret.encode("ascii")
        + b'"><p>Results for: '
        + reflected.encode("ascii")
        + b"</p>"
    )


def _deflate_size(body: bytes) -> int:
    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
    return len(compressor.compress(body) + compressor.flush())


def breach_distinguisher(
    secret: str,
    candidates: Iterable[str],
    *,
    anchor: str = 'csrf_token" value="',
    oracle: Callable[[bytes], int] | None = None,
) -> str:
    """Return the candidate prefix the compression length oracle ranks likeliest.

    This demonstrates the BREACH side channel in a single guessing step: a guess
    that extends *anchor* with the secret's true leading bytes back-references
    more of the reflected secret, so its DEFLATE output is the smallest.  It is a
    teaching distinguisher, not an extraction tool: it ranks caller-supplied
    candidates against a known *secret* and does no probing of its own.
    """
    size = oracle if oracle is not None else _deflate_size
    best: str | None = None
    best_size = 0
    for candidate in candidates:
        body = reflected_page(anchor + candidate, secret)
        length = size(body)
        if best is None or length < best_size:
            best, best_size = candidate, length
    if best is None:
        raise ValueError("candidates must be non-empty")
    return best


def mask_secret(secret: bytes, pad: bytes) -> bytes:
    """Mask *secret* as ``pad || (pad XOR secret)`` (BREACH mitigation 4).

    With a fresh random *pad* per response the reflected ciphertext changes every
    request, so the compression length oracle in :func:`breach_distinguisher`
    carries no stable signal.  *pad* must be at least as long as *secret*.
    """
    if len(pad) < len(secret):
        raise ValueError("pad must be at least as long as secret")
    masked = bytes(p ^ s for p, s in zip(pad, secret, strict=False))
    return pad[: len(secret)] + masked


async def _report(request: Request) -> Response:
    """Return a payload large enough to cross GZipMiddleware's minimum_size."""
    rows = [{"id": i, "note": "repeatable row body"} for i in range(200)]
    return JSONResponse({"rows": rows})


async def _tiny(request: Request) -> Response:
    """Return a body below minimum_size, which must not be compressed."""
    return PlainTextResponse("ok")


async def _download(request: Request) -> Response:
    """Serve a representation only in codings the server actually supports.

    When the client forbids every available coding including identity, answer
    ``406 Not Acceptable`` with a body listing the available codings (RFC 9110
    Section 15.5.7).  ``415`` with an ``Accept-Encoding`` header is the answer to
    a *request* body in an unsupported coding (RFC 9110 Section 15.5.16), not to
    a failed response negotiation.
    """
    codings = server_codings()
    chosen = negotiate_content_coding(request.headers.get("accept-encoding"), codings)
    if chosen is None:
        return PlainTextResponse(
            "no acceptable content coding; available: " + ", ".join((*codings, "identity")),
            status_code=406,
            headers={"Vary": "Accept-Encoding"},
        )
    payload = b'{"artifact": "' + b"A" * 4096 + b'"}'
    body = compress(chosen, payload)
    headers = {"Content-Type": "application/json", "Vary": "Accept-Encoding"}
    if chosen != "identity":
        headers["Content-Encoding"] = chosen
    return Response(body, headers=headers)


def build_demo_app(*, minimum_size: int = 500) -> Starlette:
    """Build the Starlette worked-example app with gzip negotiation.

    ``GZipMiddleware`` applies gzip only when the request's ``Accept-Encoding``
    allows it and the body is at least *minimum_size* bytes, and it adds
    ``Vary: Accept-Encoding`` so caches key on the negotiated coding (starlette
    1.10.0 starlette/middleware/gzip.py, GZipMiddleware).
    """
    return Starlette(
        routes=[
            Route("/report", _report),
            Route("/tiny", _tiny),
            Route("/download", _download),
        ],
        middleware=[Middleware(GZipMiddleware, minimum_size=minimum_size)],
    )
