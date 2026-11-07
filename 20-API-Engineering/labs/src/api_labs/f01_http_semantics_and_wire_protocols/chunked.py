"""HTTP/1.1 chunked transfer coding encoder and decoder (RFC 9112 Section 7.1)."""

from __future__ import annotations

_HEX_BYTES = frozenset(b"0123456789abcdefABCDEF")


def encode_chunked(body: bytes, *, chunk_size: int = 4096) -> bytes:
    """Encode *body* using HTTP/1.1 chunked transfer coding (RFC 9112 Section 7.1)."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    parts: list[bytes] = []
    for start in range(0, len(body), chunk_size):
        chunk = body[start : start + chunk_size]
        size_line = f"{len(chunk):x}\r\n".encode("ascii")
        parts.append(size_line + chunk + b"\r\n")
    parts.append(b"0\r\n\r\n")
    return b"".join(parts)


def decode_chunked(wire: bytes) -> bytes:
    """Decode an HTTP/1.1 chunked-body byte sequence into the reconstructed payload."""
    out = bytearray()
    offset = 0
    while True:
        line_end = wire.find(b"\r\n", offset)
        if line_end == -1:
            raise ValueError("unterminated chunk size line")
        size_line = wire[offset:line_end]
        semi = size_line.find(b";")
        hex_digits = size_line[:semi] if semi != -1 else size_line
        if not hex_digits or not all(b in _HEX_BYTES for b in hex_digits):
            raise ValueError("invalid chunk size hex digits")
        size = int(hex_digits, 16)
        offset = line_end + 2
        if size == 0:
            if wire[offset:] != b"\r\n":
                raise ValueError("missing or trailing bytes after terminating chunk")
            return bytes(out)
        data_end = offset + size
        if data_end + 2 > len(wire) or wire[data_end : data_end + 2] != b"\r\n":
            raise ValueError("truncated chunk data or missing chunk CRLF")
        out.extend(wire[offset:data_end])
        offset = data_end + 2
