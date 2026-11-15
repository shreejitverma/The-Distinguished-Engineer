"""A dependency-free model of the Remote Execution API v2 data plane.

Everything here follows build/bazel/remote/execution/v2/remote_execution.proto in
bazelbuild/remote-apis (main at e0c0554bfc8a, 2026-11-10) and google/bytestream/bytestream.proto.
Messages are encoded by hand in the protobuf wire format, with the canonical rules the
`Digest` comment requires: fields in tag order, no unknown or duplicate fields, and proto3
defaults omitted. The in-process servers model the RPC semantics, not gRPC transport.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

# ---------------------------------------------------------------------------
# Protobuf wire format
# ---------------------------------------------------------------------------

VARINT, LEN = 0, 2


def varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64  # int64 negatives are ten-byte two's complement varints
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def key(field_no: int, wire_type: int) -> bytes:
    return varint((field_no << 3) | wire_type)


def f_varint(field_no: int, value: int) -> bytes:
    """A proto3 scalar: omitted when it equals the default 0 or false."""
    return key(field_no, VARINT) + varint(int(value)) if value else b""


def f_bytes(field_no: int, value: bytes) -> bytes:
    return key(field_no, LEN) + varint(len(value)) + value if value else b""


def f_str(field_no: int, value: str) -> bytes:
    return f_bytes(field_no, value.encode("utf-8"))


def f_msg(field_no: int, encoded: bytes | None) -> bytes:
    """A singular message field: present (even when empty) unless None."""
    if encoded is None:
        return b""
    return key(field_no, LEN) + varint(len(encoded)) + encoded


def parse(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    """Decode one message level into (field, wire_type, value) in wire order."""
    out, i = [], 0
    while i < len(buf):
        tag, i = _read_varint(buf, i)
        fno, wt = tag >> 3, tag & 7
        if wt == VARINT:
            v, i = _read_varint(buf, i)
        elif wt == LEN:
            n, i = _read_varint(buf, i)
            if i + n > len(buf):
                raise ValueError("truncated length-delimited field")
            v, i = buf[i:i + n], i + n
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.append((fno, wt, v))
    return out


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Digest:
    hash: str
    size_bytes: int

    def encode(self) -> bytes:
        return f_str(1, self.hash) + f_varint(2, self.size_bytes)

    def short(self) -> str:
        return f"{self.hash[:12]}/{self.size_bytes}"

    @staticmethod
    def decode(buf: bytes) -> "Digest":
        h, s = "", 0
        for fno, _, v in parse(buf):
            if fno == 1:
                h = v.decode()
            elif fno == 2:
                s = v
        return Digest(h, s)


def sha256_digest(data: bytes) -> Digest:
    return Digest(hashlib.sha256(data).hexdigest(), len(data))


EMPTY = sha256_digest(b"")

# DigestFunction.Value numbers from the proto.
DIGEST_FUNCTIONS = {"SHA256": 1, "SHA1": 2, "MD5": 3, "VSO": 4, "SHA384": 5, "SHA512": 6,
                    "MURMUR3": 7, "SHA256TREE": 8, "BLAKE3": 9, "GITSHA1": 10}


def gitsha1(data: bytes) -> str:
    """DigestFunction GITSHA1: SHA-1 over "blob <size>\\0" + data, the Git blob name."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ---------------------------------------------------------------------------
# SHA256TREE (DigestFunction 8)
# ---------------------------------------------------------------------------

_K = [int(x, 16) for x in (
    "428a2f98 71374491 b5c0fbcf e9b5dba5 3956c25b 59f111f1 923f82a4 ab1c5ed5 "
    "d807aa98 12835b01 243185be 550c7dc3 72be5d74 80deb1fe 9bdc06a7 c19bf174 "
    "e49b69c1 efbe4786 0fc19dc6 240ca1cc 2de92c6f 4a7484aa 5cb0a9dc 76f988da "
    "983e5152 a831c66d b00327c8 bf597fc7 c6e00bf3 d5a79147 06ca6351 14292967 "
    "27b70a85 2e1b2138 4d2c6dfc 53380d13 650a7354 766a0abb 81c2c92e 92722c85 "
    "a2bfe8a1 a81a664b c24b8b70 c76c51a3 d192e819 d6990624 f40e3585 106aa070 "
    "19a4c116 1e376c08 2748774c 34b0bcb5 391c0cb3 4ed8aa4a 5b9cca4f 682e6ff3 "
    "748f82ee 78a5636f 84c87814 8cc70208 90befffa a4506ceb bef9a3f7 c67178f2").split()]
# Square-root fractions of the 9th to 16th primes (23..53), from the proto comment.
TREE_IV = (0xcbbb9d5d, 0x629a292a, 0x9159015a, 0x152fecd8,
           0x67332667, 0x8eb44a87, 0xdb0c2e0d, 0x47b5481d)


def _rotr(x: int, n: int) -> int:
    return ((x >> n) | (x << (32 - n))) & 0xFFFFFFFF


def sha256_block_cipher(h: tuple[int, ...], block: bytes) -> bytes:
    """One SHA-256 compression round WITHOUT the Davies-Meyer feed-forward addition."""
    w = list(struct.unpack(">16I", block))
    for t in range(16, 64):
        s0 = _rotr(w[t - 15], 7) ^ _rotr(w[t - 15], 18) ^ (w[t - 15] >> 3)
        s1 = _rotr(w[t - 2], 17) ^ _rotr(w[t - 2], 19) ^ (w[t - 2] >> 10)
        w.append((w[t - 16] + s0 + w[t - 7] + s1) & 0xFFFFFFFF)
    a, b, c, d, e, f, g, hh = h
    for t in range(64):
        t1 = (hh + (_rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)) + ((e & f) ^ (~e & g)) + _K[t] + w[t]) & 0xFFFFFFFF
        t2 = ((_rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) & 0xFFFFFFFF
        a, b, c, d, e, f, g, hh = (t1 + t2) & 0xFFFFFFFF, a, b, c, (d + t1) & 0xFFFFFFFF, e, f, g
    return struct.pack(">8I", a, b, c, d, e, f, g, hh)


def sha256tree(data: bytes) -> bytes:
    if len(data) <= 1024:
        return hashlib.sha256(data).digest()
    m = 1 << ((len(data) - 1).bit_length() - 1)  # largest power of two with 0 < n <= m
    return sha256_block_cipher(TREE_IV, sha256tree(data[:m]) + sha256tree(data[m:]))


# ---------------------------------------------------------------------------
# REAPI messages
# ---------------------------------------------------------------------------


def enc_property(name: str, value: str) -> bytes:
    return f_str(1, name) + f_str(2, value)


def enc_platform(props: Iterable[tuple[str, str]]) -> bytes | None:
    props = list(props)
    if not props:
        return None
    return b"".join(f_msg(1, enc_property(n, v)) for n, v in sorted(props))


def enc_command(args: list[str], env: list[tuple[str, str]], output_paths: list[str],
                working_directory: str = "", sort_env: bool = True) -> bytes:
    """Fields in tag order: arguments (1), environment_variables (2), working_directory (6),
    output_paths (7). The deprecated output_files (3), output_directories (4), and platform (5)
    are never written, as Bazel 9.3.2 does not write them either."""
    if sort_env:
        env = sorted(env)
    out = b"".join(f_str(1, a) for a in args)
    out += b"".join(f_msg(2, enc_property(n, v)) for n, v in env)
    out += f_str(6, working_directory)
    out += b"".join(f_str(7, p) for p in sorted(set(output_paths)))
    return out


def enc_cache_salt(may_be_executed_remotely: bool = True, workspace: str = "") -> bytes:
    """Bazel's tools.protos.CacheSalt (src/main/protobuf/cache_salt.proto at 9.3.2)."""
    return f_varint(1, may_be_executed_remotely) + f_str(2, workspace)


def enc_action(command: Digest, input_root: Digest, timeout_s: int = 0, do_not_cache: bool = False,
               salt: bytes = b"", platform: bytes | None = None) -> bytes:
    out = f_msg(1, command.encode()) + f_msg(2, input_root.encode())
    if timeout_s:
        out += f_msg(6, f_varint(1, timeout_s))  # google.protobuf.Duration{seconds}
    out += f_varint(7, do_not_cache) + f_bytes(9, salt) + f_msg(10, platform)
    return out


def enc_file_node(name: str, digest: Digest, is_executable: bool) -> bytes:
    return f_str(1, name) + f_msg(2, digest.encode()) + f_varint(4, is_executable)


def enc_dir_node(name: str, digest: Digest) -> bytes:
    return f_str(1, name) + f_msg(2, digest.encode())


def enc_symlink_node(name: str, target: str) -> bytes:
    return f_str(1, name) + f_str(2, target)


@dataclass
class DirBuilder:
    files: dict[str, tuple[Digest, bool]] = field(default_factory=dict)
    dirs: dict[str, "DirBuilder"] = field(default_factory=dict)
    symlinks: dict[str, str] = field(default_factory=dict)


def encode_directory(d: DirBuilder, blobs: dict[Digest, bytes]) -> bytes:
    """Encode bottom-up; every child Directory is digested before its parent can be."""
    out = b""
    for name in sorted(d.files):  # Python str order is code point order
        dg, ex = d.files[name]
        out += f_msg(1, enc_file_node(name, dg, ex))
    for name in sorted(d.dirs):
        child = encode_directory(d.dirs[name], blobs)
        cd = sha256_digest(child)
        blobs[cd] = child
        out += f_msg(2, enc_dir_node(name, cd))
    for name in sorted(d.symlinks):
        out += f_msg(3, enc_symlink_node(name, d.symlinks[name]))
    return out


def merkle_root(entries: Iterable[tuple[str, Digest, bool]]) -> tuple[Digest, dict[Digest, bytes]]:
    """Build an input root from (path, content digest, is_executable) triples."""
    root = DirBuilder()
    for path, dg, ex in entries:
        parts = path.split("/")
        node = root
        for seg in parts[:-1]:
            node = node.dirs.setdefault(seg, DirBuilder())
        node.files[parts[-1]] = (dg, ex)
    blobs: dict[Digest, bytes] = {}
    enc = encode_directory(root, blobs)
    rd = sha256_digest(enc)
    blobs[rd] = enc
    return rd, blobs


def decode_directory(buf: bytes) -> dict[str, list]:
    out: dict[str, list] = {"files": [], "dirs": [], "symlinks": []}
    for fno, _, v in parse(buf):
        sub = {f: val for f, _, val in parse(v)} if fno in (1, 2, 3) else {}
        if fno == 1:
            out["files"].append((sub[1].decode(), Digest.decode(sub.get(2, b"")), bool(sub.get(4, 0))))
        elif fno == 2:
            out["dirs"].append((sub[1].decode(), Digest.decode(sub.get(2, b""))))
        elif fno == 3:
            out["symlinks"].append((sub[1].decode(), sub.get(2, b"").decode()))
    return out


def directory_is_canonical(buf: bytes) -> str | None:
    """Return why a Directory violates canonical form, or None."""
    d = decode_directory(buf)
    last_fno = 0
    for fno, _, _ in parse(buf):
        if fno < last_fno:
            return "fields not in tag order"
        last_fno = fno
    for kind in ("files", "dirs", "symlinks"):
        names = [e[0] for e in d[kind]]
        if names != sorted(names):
            return f"{kind} not sorted by name"
        if any("/" in n or n in ("", ".", "..") for n in names):
            return f"{kind} name is not a single path segment"
    allnames = [e[0] for k in d for e in d[k]]
    if len(allnames) != len(set(allnames)):
        return "duplicate child name"
    return None


def command_is_canonical(buf: bytes) -> str | None:
    env = [(dict((f, v) for f, _, v in parse(v))[1]) for f, _, v in parse(buf) if f == 2]
    if env != sorted(env):
        return "environment_variables not sorted by name"
    outs = [v for f, _, v in parse(buf) if f == 7]
    if outs != sorted(set(outs)):
        return "output_paths not sorted and deduplicated"
    return None


def enc_tree(root: bytes, children: list[bytes]) -> bytes:
    return f_msg(1, root) + b"".join(f_msg(2, c) for c in children)


def tree_records(root_dg: Digest, blobs: dict[Digest, bytes]) -> tuple[bytes, list[bytes]]:
    """Topologically sorted, deduplicated children: parents before children, root first."""
    seen, order, queue = {root_dg}, [], [root_dg]
    while queue:
        dg = queue.pop(0)
        for _, child in decode_directory(blobs[dg])["dirs"]:
            if child not in seen:
                seen.add(child)
                order.append(blobs[child])
                queue.append(child)
    return blobs[root_dg], order


def enc_output_file(path: str, digest: Digest, is_executable: bool) -> bytes:
    return f_str(1, path) + f_msg(2, digest.encode()) + f_varint(4, is_executable)


def enc_action_result(outputs: list[tuple[str, Digest, bool]], exit_code: int, stdout: Digest,
                      stderr: Digest, worker: str) -> bytes:
    out = b"".join(f_msg(2, enc_output_file(p, d, x)) for p, d, x in outputs)
    out += f_varint(4, exit_code) + f_msg(6, stdout.encode()) + f_msg(8, stderr.encode())
    out += f_msg(9, f_str(1, worker))
    return out


def decode_action_result(buf: bytes) -> dict:
    res = {"outputs": [], "exit_code": 0, "stdout": None, "stderr": None, "metadata_fields": []}
    for fno, _, v in parse(buf):
        if fno == 2:
            sub = {f: val for f, _, val in parse(v)}
            res["outputs"].append((sub[1].decode(), Digest.decode(sub.get(2, b"")), bool(sub.get(4, 0))))
        elif fno == 4:
            res["exit_code"] = v
        elif fno == 6:
            res["stdout"] = Digest.decode(v)
        elif fno == 8:
            res["stderr"] = Digest.decode(v)
        elif fno == 9:
            res["metadata_fields"] = sorted({f for f, _, _ in parse(v)})
            res["worker"] = next((val.decode() for f, _, val in parse(v) if f == 1), "")
    return res


def decode_action(buf: bytes) -> dict:
    a = {"command": None, "input_root": None, "timeout_s": 0, "do_not_cache": False, "salt": b"",
         "platform": b""}
    for fno, _, v in parse(buf):
        if fno == 1:
            a["command"] = Digest.decode(v)
        elif fno == 2:
            a["input_root"] = Digest.decode(v)
        elif fno == 6:
            a["timeout_s"] = dict((f, val) for f, _, val in parse(v)).get(1, 0)
        elif fno == 7:
            a["do_not_cache"] = bool(v)
        elif fno == 9:
            a["salt"] = v
        elif fno == 10:
            a["platform"] = v
    return a


def decode_command(buf: bytes) -> dict:
    c = {"args": [], "env": [], "output_paths": [], "working_directory": ""}
    for fno, _, v in parse(buf):
        if fno == 1:
            c["args"].append(v.decode())
        elif fno == 2:
            sub = dict((f, val) for f, _, val in parse(v))
            c["env"].append((sub.get(1, b"").decode(), sub.get(2, b"").decode()))
        elif fno == 6:
            c["working_directory"] = v.decode()
        elif fno == 7:
            c["output_paths"].append(v.decode())
    return c


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------


class RpcError(Exception):
    def __init__(self, code: str, message: str, violations: list[str] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.violations = violations or []


KEYWORDS = {"blobs", "uploads", "actions", "actionResults", "operations", "capabilities",
            "compressed-blobs"}
def _deflate(b: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)  # RFC 1951 raw deflate, no gzip/zlib header
    return c.compress(b) + c.flush()


def _inflate(b: bytes) -> bytes:
    return zlib.decompress(b, wbits=-15)


# Compressor.Value DEFLATE (2); ZSTD (1) would need a zstd module the lab avoids for portability.
COMPRESSORS = {"deflate": (_deflate, _inflate)}
# Functions numbered above 7 must be named in the resource name; 1 to 7 are inferred from hash length.
NAMED_FUNCTIONS = {n.lower() for n, v in DIGEST_FUNCTIONS.items() if v > 7}


def parse_resource_name(name: str, upload: bool) -> dict:
    """Split a ByteStream resource name into instance, (uuid), compressor, function, digest."""
    segs = name.split("/")
    marker = ("uploads",) if upload else ("blobs", "compressed-blobs")
    idx = next((i for i, s in enumerate(segs) if s in marker), None)
    if idx is None:
        raise RpcError("INVALID_ARGUMENT", f"no {'/'.join(marker)} segment in {name!r}")
    instance = "/".join(segs[:idx])
    if any(s in KEYWORDS for s in segs[:idx]):
        raise RpcError("INVALID_ARGUMENT", "instance name contains a reserved keyword segment")
    rest = segs[idx + 1:]
    uuid = rest.pop(0) if upload else None
    kind = rest.pop(0) if upload else segs[idx]
    if kind not in ("blobs", "compressed-blobs"):
        raise RpcError("INVALID_ARGUMENT", f"unexpected segment {kind!r}")
    compressor = rest.pop(0) if kind == "compressed-blobs" else "identity"
    if compressor != "identity" and compressor not in COMPRESSORS:
        raise RpcError("INVALID_ARGUMENT", f"compressor {compressor!r} not advertised")
    func = rest.pop(0) if rest and rest[0] in NAMED_FUNCTIONS else "inferred"
    if len(rest) < 2 or not rest[1].isdigit():
        raise RpcError("INVALID_ARGUMENT", f"no hash/size in {name!r}")
    h, size = rest[0], int(rest[1])
    return {"instance": instance, "uuid": uuid, "compressor": compressor, "function": func,
            "digest": Digest(h, size), "metadata": "/".join(rest[2:])}


@dataclass
class Stats:
    find_missing_calls: int = 0
    batch_update_calls: int = 0
    bytestream_writes: int = 0
    bytestream_write_msgs: int = 0
    bytes_uploaded: int = 0
    bytes_downloaded: int = 0
    batch_read_calls: int = 0
    bytestream_reads: int = 0


class Cas:
    def __init__(self, max_batch_total_size_bytes: int = 0) -> None:
        self.blobs: dict[tuple[str, Digest], bytes] = {}
        self.max_batch = max_batch_total_size_bytes
        self.partial: dict[str, bytearray] = {}
        self.stats = Stats()

    # FindMissingBlobs
    def find_missing(self, instance: str, digests: Iterable[Digest]) -> list[Digest]:
        self.stats.find_missing_calls += 1
        return [d for d in dict.fromkeys(digests) if not self.has(instance, d)]

    def has(self, instance: str, d: Digest) -> bool:
        return d.size_bytes == 0 or (instance, d) in self.blobs  # empty blobs always exist

    def evict(self, instance: str, d: Digest) -> None:
        self.blobs.pop((instance, d), None)

    def _store(self, instance: str, d: Digest, data: bytes) -> None:
        if sha256_digest(data) != d:
            raise RpcError("INVALID_ARGUMENT", f"digest mismatch for {d.short()}")
        self.blobs[(instance, d)] = data

    # BatchUpdateBlobs
    def batch_update(self, instance: str, reqs: list[tuple[Digest, bytes]]) -> list[str]:
        self.stats.batch_update_calls += 1
        total = sum(len(b) for _, b in reqs)
        if self.max_batch and total > self.max_batch:
            raise RpcError("INVALID_ARGUMENT", f"batch of {total} bytes exceeds {self.max_batch}")
        statuses = []
        for d, data in reqs:
            try:
                self._store(instance, d, data)
                self.stats.bytes_uploaded += len(data)
                statuses.append("OK")
            except RpcError as e:
                statuses.append(e.code)
        return statuses

    # BatchReadBlobs
    def batch_read(self, instance: str, digests: list[Digest]) -> list[tuple[Digest, bytes | None]]:
        self.stats.batch_read_calls += 1
        out = []
        for d in digests:
            data = b"" if d.size_bytes == 0 else self.blobs.get((instance, d))
            if data is not None:
                self.stats.bytes_downloaded += len(data)
            out.append((d, data))
        return out

    # ByteStream.Write
    def write(self, requests: Iterator[tuple[str, int, bool, bytes]]) -> int:
        self.stats.bytestream_writes += 1
        first = next(requests)
        name = first[0]
        rn = parse_resource_name(name, upload=True)
        inst, d, comp = rn["instance"], rn["digest"], rn["compressor"]
        if self.has(inst, d):  # someone else already finished: stop early, report done
            return -1 if comp != "identity" else d.size_bytes
        buf = self.partial.setdefault(name, bytearray())
        expected_offset = len(buf)
        for rname, offset, finish, data in _chain(first, requests):
            self.stats.bytestream_write_msgs += 1
            if rname and rname != name:
                raise RpcError("INVALID_ARGUMENT", "resource_name changed mid-stream")
            if offset != expected_offset:
                raise RpcError("INVALID_ARGUMENT", f"write_offset {offset}, expected {expected_offset}")
            buf += data
            self.stats.bytes_uploaded += len(data)
            expected_offset += len(data)
            if finish:
                raw = COMPRESSORS[comp][1](bytes(buf)) if comp != "identity" else bytes(buf)
                del self.partial[name]
                self._store(inst, d, raw)
                return -1 if comp != "identity" else d.size_bytes
        return len(buf)  # stream ended without finish_write: partial, resumable

    def query_write_status(self, name: str) -> tuple[int, bool]:
        rn = parse_resource_name(name, upload=True)
        if self.has(rn["instance"], rn["digest"]):
            return rn["digest"].size_bytes, True
        return len(self.partial.get(name, b"")), False

    # ByteStream.Read
    def read(self, name: str, read_offset: int = 0, read_limit: int = 0, chunk: int = 16384) -> Iterator[bytes]:
        rn = parse_resource_name(name, upload=False)
        if rn["compressor"] != "identity" and read_limit:
            raise RpcError("INVALID_ARGUMENT", "read_limit must be 0 for compressed-blobs")
        data = b"" if rn["digest"].size_bytes == 0 else self.blobs.get((rn["instance"], rn["digest"]))
        if data is None:
            raise RpcError("NOT_FOUND", f"blob {rn['digest'].short()} not in CAS")
        if read_offset < 0 or read_offset > len(data):
            raise RpcError("OUT_OF_RANGE", "read_offset outside the blob")
        self.stats.bytestream_reads += 1
        view = data[read_offset:read_offset + read_limit] if read_limit else data[read_offset:]
        if rn["compressor"] != "identity":
            view = COMPRESSORS[rn["compressor"]][0](view)
        self.stats.bytes_downloaded += len(view)
        for i in range(0, len(view), chunk):
            yield view[i:i + chunk]


def _chain(first, rest):
    yield first
    yield from rest


class ActionCache:
    """AC keyed by (instance, action digest); optional writer ACL and completeness checking."""

    def __init__(self, cas: Cas, writers: set[str] | None = None, check_completeness: bool = False) -> None:
        self.cas = cas
        self.entries: dict[tuple[str, Digest], bytes] = {}
        self.writers = writers
        self.check_completeness = check_completeness

    def get(self, instance: str, action: Digest) -> bytes:
        res = self.entries.get((instance, action))
        if res is None:
            raise RpcError("NOT_FOUND", f"no ActionResult for {action.short()}")
        if self.check_completeness:
            r = decode_action_result(res)
            needed = [d for _, d, _ in r["outputs"]] + [r["stdout"], r["stderr"]]
            if self.cas.find_missing(instance, [d for d in needed if d]):
                raise RpcError("NOT_FOUND", "ActionResult references evicted blobs")
        return res

    def update(self, identity: str, instance: str, action: Digest, result: bytes) -> None:
        if self.writers is not None and identity not in self.writers:
            raise RpcError("PERMISSION_DENIED", f"{identity!r} may not write the action cache")
        a_blob = self.cas.blobs.get((instance, action))
        if a_blob is None or not self.cas.has(instance, decode_action(a_blob)["command"]):
            raise RpcError("FAILED_PRECONDITION", "Action and Command must be uploaded first")
        self.entries[(instance, action)] = result


Tool = Callable[[list[str], dict[str, bytes]], tuple[int, dict[str, bytes], bytes]]


class Executor:
    """Execution service with an in-process worker whose host tools are `tools`."""

    def __init__(self, cas: Cas, ac: ActionCache, tools: dict[str, Tool], worker: str = "worker-1") -> None:
        self.cas, self.ac, self.tools, self.worker = cas, ac, tools, worker
        self.executions = 0

    def execute(self, instance: str, action_dg: Digest, skip_cache_lookup: bool = False) -> tuple[list[str], dict]:
        stages = ["CACHE_CHECK"]
        if not skip_cache_lookup:
            try:
                res = self.ac.get(instance, action_dg)
                stages.append("COMPLETED")
                return stages, {"cached_result": True, "result": decode_action_result(res), "status": "OK"}
            except RpcError as e:
                if e.code != "NOT_FOUND":
                    raise
        a_blob = self.cas.blobs.get((instance, action_dg))
        if a_blob is None:
            raise RpcError("FAILED_PRECONDITION", "missing Action",
                           [f"blobs/{action_dg.hash}/{action_dg.size_bytes}"])
        action = decode_action(a_blob)
        missing = self._missing_inputs(instance, action)
        if missing:
            raise RpcError("FAILED_PRECONDITION", "missing inputs",
                           [f"blobs/{d.hash}/{d.size_bytes}" for d in missing])
        stages += ["QUEUED", "EXECUTING"]
        cmd = decode_command(self.cas.blobs[(instance, action["command"])]) \
            if action["command"].size_bytes else decode_command(b"")
        files = self._materialize(instance, action["input_root"], "")
        tool = self.tools.get(cmd["args"][0])
        self.executions += 1
        if tool is None:
            stages.append("COMPLETED")
            return stages, {"cached_result": False, "status": "OK", "result": {"exit_code": 127, "outputs": []}}
        code, produced, stdout = tool(cmd["args"], files)
        outs = []
        for p in sorted(cmd["output_paths"]):
            if p in produced:
                d = sha256_digest(produced[p])
                self.cas._store(instance, d, produced[p])
                outs.append((p, d, True))
        sd = sha256_digest(stdout)
        if stdout:
            self.cas._store(instance, sd, stdout)
        result = enc_action_result(outs, code, sd, EMPTY, self.worker)
        if code == 0 and not action["do_not_cache"]:
            self.ac.entries[(instance, action_dg)] = result  # the worker is a trusted writer
        stages.append("COMPLETED")
        return stages, {"cached_result": False, "status": "OK", "result": decode_action_result(result)}

    def _missing_inputs(self, instance: str, action: dict) -> list[Digest]:
        need = [action["command"], action["input_root"]]
        missing = [d for d in need if not self.cas.has(instance, d)]
        if missing:
            return missing
        stack, blobs = [action["input_root"]], []
        while stack:
            d = stack.pop()
            if d.size_bytes == 0:
                continue
            node = decode_directory(self.cas.blobs[(instance, d)])
            blobs += [fd for _, fd, _ in node["files"]]
            for _, cd in node["dirs"]:
                if not self.cas.has(instance, cd):
                    return [cd]
                stack.append(cd)
        return self.cas.find_missing(instance, blobs)

    def _materialize(self, instance: str, root: Digest, prefix: str) -> dict[str, bytes]:
        if root.size_bytes == 0:
            return {}
        node = decode_directory(self.cas.blobs[(instance, root)])
        out = {}
        for name, d, _ in node["files"]:
            out[prefix + name] = b"" if d.size_bytes == 0 else self.cas.blobs[(instance, d)]
        for name, d in node["dirs"]:
            out.update(self._materialize(instance, d, prefix + name + "/"))
        return out


# ---------------------------------------------------------------------------
# Client upload path
# ---------------------------------------------------------------------------


def upload_blobs(cas: Cas, instance: str, blobs: dict[Digest, bytes], batch_limit: int,
                 chunk: int = 16384, uuid: str = "00000000-0000-4000-8000-000000000000") -> dict:
    """FindMissingBlobs, then batch small blobs and stream large ones, as the CAS service comment
    recommends. Bazel 9.3.2's GrpcCacheClient does not use the batch RPCs at all: after
    FindMissingBlobs it sends every missing blob with ByteStream.Write in 16 KiB chunks."""
    missing = cas.find_missing(instance, blobs.keys())
    small = [d for d in missing if d.size_bytes <= batch_limit]
    large = [d for d in missing if d.size_bytes > batch_limit]
    batches, cur, cur_size = [], [], 0
    for d in sorted(small):
        if cur and cur_size + d.size_bytes > batch_limit:
            batches.append(cur)
            cur, cur_size = [], 0
        cur.append(d)
        cur_size += d.size_bytes
    if cur:
        batches.append(cur)
    for b in batches:
        cas.batch_update(instance, [(d, blobs[d]) for d in b])
    for d in sorted(large):
        name = (instance + "/" if instance else "") + f"uploads/{uuid}/blobs/{d.hash}/{d.size_bytes}"
        cas.write(write_requests(name, blobs[d], chunk))
    return {"checked": len(blobs), "missing": len(missing), "batched": len(small),
            "batches": len(batches), "streamed": len(large)}


def write_requests(name: str, data: bytes, chunk: int, start: int = 0,
                   stop_after: int | None = None) -> Iterator[tuple[str, int, bool, bytes]]:
    off, first = start, True
    while True:
        piece = data[off:off + chunk]
        finish = off + len(piece) >= len(data)
        yield (name if first else "", off, finish, piece)
        first = False
        off += len(piece)
        if finish or (stop_after is not None and off >= stop_after):
            return
