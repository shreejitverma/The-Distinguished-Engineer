"""Remote Execution API v2 lab: replay real Bazel 9.3.2 cache entries, then exercise each RPC.

Every check raises AssertionError (or RpcError) when the mechanism it demonstrates is absent,
so a clean run is the proof; the printed lines are derived facts, not timings.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import reapi as r
from reapi import Digest, RpcError, sha256_digest

HERE = Path(__file__).resolve().parent


def section(title: str) -> None:
    print(f"\n== {title}")


def expect_error(code: str, fn, *args, **kwargs) -> RpcError:
    try:
        out = fn(*args, **kwargs)
        if hasattr(out, "__next__"):
            list(out)
    except RpcError as e:
        assert e.code == code, f"expected {code}, got {e.code}: {e}"
        return e
    raise AssertionError(f"expected {code}, call succeeded")


def stream(seed: bytes, n: int) -> bytes:
    """Deterministic pseudo-random bytes: SHA-256 in counter mode, identical on every platform."""
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(seed + i.to_bytes(8, "big")).digest()
        i += 1
    return bytes(out[:n])


# ---------------------------------------------------------------------------
# 1. Real Bazel bytes
# ---------------------------------------------------------------------------


def bazel_replay() -> None:
    section("1. Replaying Bazel 9.3.2 disk-cache entries (fixtures/bazel, captured on Darwin-arm64)")
    m = json.loads((HERE / "fixtures/bazel/manifest.json").read_text())
    cas = {h: bytes.fromhex(x) for h, x in m["cas_blobs_hex"].items()}
    for h, blob in cas.items():
        assert hashlib.sha256(blob).hexdigest() == h, "a disk-cache cas/ file is not content-addressed"
    print(f"fixture tool: {m['bazel']}; cas/ blobs: {len(cas)}, all named by their SHA-256")
    ac_names = [p for p in m["disk_cache_files"] if p.startswith("ac/")]
    rows, parts = [], {}
    for variant in ("base", "env", "input", "hit"):
        rec = m["variants"][variant]
        vdir = HERE / "fixtures/bazel" / variant
        action_blob = (vdir / "action.bin").read_bytes()
        logged = Digest(rec["digest"]["hash"], int(rec["digest"]["sizeBytes"]))
        assert sha256_digest(action_blob) == logged, "execution log digest != SHA-256 of the Action"
        action = r.decode_action(action_blob)

        # Rebuild the Command from the execution log fields and compare bytes with Bazel's.
        env = [(e["name"], e["value"]) for e in rec["environmentVariables"]]
        cmd = r.enc_command(rec["commandArgs"], env, rec["listedOutputs"])
        assert cmd == cas[action["command"].hash], f"{variant}: Command bytes differ from Bazel's"

        # Rebuild the input root Merkle tree. Bazel marks every input file executable
        # (MerkleTreeComputer.addFile at 9.3.2), so is_executable=True for all of them.
        entries = [(i["path"], Digest(i["digest"]["hash"], int(i["digest"]["sizeBytes"])), True)
                   for i in rec["inputs"]]
        root, dir_blobs = r.merkle_root(entries)
        assert root == action["input_root"], f"{variant}: input root digest differs from Bazel's"
        wrong_bit, _ = r.merkle_root([(p, d, False) for p, d, _ in entries])
        assert wrong_bit != root, "is_executable must be part of the key"

        salt = r.enc_cache_salt(may_be_executed_remotely=True)
        assert action["salt"] == salt, f"{variant}: unexpected salt {action['salt'].hex()}"
        rebuilt = r.enc_action(sha256_digest(cmd), root, salt=salt)
        assert rebuilt == action_blob, f"{variant}: Action bytes differ from Bazel's"

        result = r.decode_action_result((vdir / "action_result.bin").read_bytes())
        (out_path, out_dg, out_exec), = result["outputs"]
        assert out_dg.hash in cas and sha256_digest(cas[out_dg.hash]) == out_dg
        assert result["stdout"] == r.EMPTY and result["stderr"] == r.EMPTY
        assert f"ac/{logged.hash[:2]}/{logged.hash}" in ac_names
        parts[variant] = (sha256_digest(cmd), root, len(dir_blobs))
        rows.append((variant, logged.short(), sha256_digest(cmd).short(), root.short(),
                     rec["runner"], cas[out_dg.hash].decode().strip()))

    print("Action, Command, and input root rebuilt byte-for-byte for 4/4 variants")
    print(f"{'variant':8} {'action':18} {'command':18} {'input root':18} {'runner':15} output")
    for row in rows:
        print(f"{row[0]:8} {row[1]:18} {row[2]:18} {row[3]:18} {row[4]:15} {row[5]}")
    base = parts["base"]
    for v in ("env", "input", "hit"):
        changed = [n for n, a, b in zip(("command", "input root"), parts[v][:2], base[:2]) if a != b]
        print(f"  {v} vs base: changed = {', '.join(changed) or 'nothing (same action key)'}")
    assert base[2] == 6
    print(f"input root holds {base[2]} Directory messages for 2 input files "
          f"(root, external, external/bazel_tools, .../tools, .../genrule, pkg)")
    print(f"Action salt bytes: {r.enc_cache_salt().hex()} = CacheSalt{{may_be_executed_remotely: true}}")
    print(f"ActionResult execution_metadata fields set: {result['metadata_fields']} (timestamps; no worker name)")
    print(f"ac/ entries: {len(ac_names)} (keyed by action digest, value is an ActionResult, not content-addressed)")


# ---------------------------------------------------------------------------
# 2. Canonical form
# ---------------------------------------------------------------------------


def canonical_form() -> None:
    section("2. Canonical encoding is part of the cache key")
    empty_dir = r.encode_directory(r.DirBuilder(), {})
    assert empty_dir == b"" and sha256_digest(empty_dir) == r.EMPTY
    print(f"empty Directory encodes to 0 bytes; its digest is the empty blob {r.EMPTY.short()}")
    args, outs = ["upper", "in.txt", "out.txt"], ["out.txt"]
    good = r.enc_command(args, [("A", "1"), ("B", "2")], outs)
    bad = r.enc_command(args, [("B", "2"), ("A", "1")], outs, sort_env=False)
    assert sha256_digest(good) != sha256_digest(bad)
    assert r.command_is_canonical(good) is None
    print(f"env sorted   -> command {sha256_digest(good).short()}")
    print(f"env unsorted -> command {sha256_digest(bad).short()} ({r.command_is_canonical(bad)})")
    d = r.DirBuilder()
    d.files["b.txt"] = (r.EMPTY, False)
    d.files["a.txt"] = (r.EMPTY, False)
    sorted_dir = r.encode_directory(d, {})
    unsorted = (r.f_msg(1, r.enc_file_node("b.txt", r.EMPTY, False))
                + r.f_msg(1, r.enc_file_node("a.txt", r.EMPTY, False)))
    assert r.directory_is_canonical(sorted_dir) is None
    print(f"Directory [b.txt, a.txt]: {r.directory_is_canonical(unsorted)}; digest differs: "
          f"{sha256_digest(sorted_dir) != sha256_digest(unsorted)}")
    c, root = sha256_digest(good), r.EMPTY
    variants = {"timeout unset": r.enc_action(c, root), "timeout 60s": r.enc_action(c, root, timeout_s=60),
                "timeout 61s": r.enc_action(c, root, timeout_s=61),
                "salt b'v2'": r.enc_action(c, root, salt=b"v2"),
                "platform OSFamily=linux": r.enc_action(c, root, platform=r.enc_platform([("OSFamily", "linux")]))}
    digests = {k: sha256_digest(v) for k, v in variants.items()}
    assert len(set(digests.values())) == len(digests)
    for k, v in digests.items():
        print(f"  action with {k:24} -> {v.short()}")


# ---------------------------------------------------------------------------
# 3. Merkle tree incrementality and dedup
# ---------------------------------------------------------------------------


def merkle_incremental() -> None:
    section("3. Merkle input roots: one changed file re-hashes one path to the root")
    files = {f"src/m{a}/s{b}/f{c}.cc": stream(f"{a}{b}{c}".encode(), 64)
             for a in range(10) for b in range(10) for c in range(10)}

    def build(fs):
        blobs = {sha256_digest(v): v for v in fs.values()}
        root, dirs = r.merkle_root((p, sha256_digest(v), False) for p, v in fs.items())
        blobs.update(dirs)
        return root, dirs, blobs

    root1, dirs1, blobs1 = build(files)
    cas = r.Cas()
    stats1 = r.upload_blobs(cas, "", blobs1, batch_limit=4 << 20)
    files["src/m3/s7/f5.cc"] = b"changed\n"
    root2, dirs2, blobs2 = build(files)
    missing = cas.find_missing("", blobs2.keys())
    new_dirs = [d for d in missing if d in dirs2]
    assert root1 != root2 and len(new_dirs) == 4 and len(missing) == 5
    assert len(dirs1) == 112
    print(f"1000 files under src/m0..m9/s0..s9/: {len(dirs1)} Directory blobs (root, src, 10 mN, 100 sN)")
    print(f"first upload: {stats1['checked']} digests checked, {stats1['missing']} missing, "
          f"{stats1['batches']} BatchUpdateBlobs call(s)")
    print(f"after editing src/m3/s7/f5.cc: FindMissingBlobs reports {len(missing)} missing = "
          f"1 file + {len(new_dirs)} Directory blobs (s7, m3, src, root)")
    same = {"a/x.h": b"same\n", "b/x.h": b"same\n"}
    _, d = r.merkle_root((p, sha256_digest(v), False) for p, v in same.items())
    assert len(d) == 2  # a/ and b/ hash identically, so only one child blob plus the root
    print(f"two identical subdirectories a/ and b/ share one Directory blob: {len(d)} blobs incl. root")


# ---------------------------------------------------------------------------
# 4. CAS batching and FindMissingBlobs sizing
# ---------------------------------------------------------------------------


def cas_batching() -> None:
    section("4. CAS uploads: FindMissingBlobs, BatchUpdateBlobs limit, ByteStream for large blobs")
    limit = 64 * 1024
    sizes = [100, 5_000, 30_000, 40_000, 60_000, 70_000, 300_000]
    blobs = {sha256_digest(b): b for b in (stream(str(s).encode(), s) for s in sizes)}
    cas = r.Cas(max_batch_total_size_bytes=limit)
    st = r.upload_blobs(cas, "main", blobs, batch_limit=limit)
    assert st["streamed"] == 2 and st["batches"] == 3
    print(f"max_batch_total_size_bytes={limit}: {st['batched']} small blobs in {st['batches']} batches, "
          f"{st['streamed']} blobs over the limit via ByteStream.Write")
    e = expect_error("INVALID_ARGUMENT", cas.batch_update, "main", [(dg, b) for dg, b in blobs.items()])
    print(f"one batch of all {len(blobs)} blobs ({sum(sizes):,} bytes) -> {e.code}")
    st2 = r.upload_blobs(cas, "main", blobs, batch_limit=limit)
    assert st2["missing"] == 0
    print(f"second upload of the same blobs: {st2['missing']} missing, 0 bytes sent")
    other = cas.find_missing("other-instance", blobs.keys())
    assert len(other) == len(blobs)
    print(f"instance_name 'other-instance' sees {len(other)}/{len(blobs)} missing: instances are separate namespaces")
    bad = [(sha256_digest(b"real"), b"fake")]
    print(f"BatchUpdateBlobs with data not matching its digest -> per-blob status {cas.batch_update('main', bad)[0]}")
    # Bazel 9.3.2 GrpcCacheClient.computeMaxMissingBlobsDigestsPerMessage, with its 1 MiB
    # maxOutboundMessageSize and the 4 MiB gRPC default receive limit for comparison.
    overhead = len(r.f_varint(3, 1))                       # FindMissingBlobsRequest{digest_function=SHA256}
    tag = len(r.f_msg(2, b""))                              # field tag + zero length of an empty Digest
    digest_size = len(sha256_digest(b"\x01").encode()) + tag
    for limit_bytes in (1 << 20, 4 << 20):
        print(f"FindMissingBlobs digests per {limit_bytes >> 20} MiB request: "
              f"({limit_bytes} - {overhead}) // {digest_size} = {(limit_bytes - overhead) // digest_size}")


# ---------------------------------------------------------------------------
# 5. ByteStream
# ---------------------------------------------------------------------------


def bytestream() -> None:
    section("5. ByteStream Write/Read: resource names, offsets, resume, dedup, compression")
    cas = r.Cas()
    data = stream(b"bytestream", 100_000)
    d = sha256_digest(data)
    uuid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    name = f"main/uploads/{uuid}/blobs/{d.hash}/{d.size_bytes}"
    print(f"upload resource: main/uploads/{{uuid}}/blobs/{d.hash[:12]}.../{d.size_bytes}")
    committed = cas.write(r.write_requests(name, data, 16384, stop_after=49152))
    q = cas.query_write_status(name)
    assert committed == 49152 and q == (49152, False)
    print(f"stream broken after 3 x 16 KiB messages: committed_size={committed}, "
          f"QueryWriteStatus={{committed_size: {q[0]}, complete: {q[1]}}}")
    e = expect_error("INVALID_ARGUMENT", cas.write, r.write_requests(name, data, 16384, start=0))
    print(f"resume at write_offset 0 instead of 49152 -> {e.code}")
    committed = cas.write(r.write_requests(name, data, 16384, start=49152))
    assert committed == 100_000 and cas.has("main", d)
    print(f"resume at write_offset 49152: committed_size={committed}, "
          f"messages so far={cas.stats.bytestream_write_msgs}, bytes sent={cas.stats.bytes_uploaded}")
    name2 = f"main/uploads/{uuid.replace('3fa8', '0000')}/blobs/{d.hash}/{d.size_bytes}"
    before = cas.stats.bytes_uploaded
    committed = cas.write(r.write_requests(name2, data, 16384))
    assert committed == 100_000 and cas.stats.bytes_uploaded == before
    print(f"second client uploads the same blob: committed_size={committed} at once, 0 bytes accepted")
    fake = sha256_digest(b"x" * 100)
    e = expect_error("INVALID_ARGUMENT", cas.write,
                     r.write_requests(f"uploads/{uuid}/blobs/{fake.hash}/100", b"y" * 100, 16384))
    print(f"upload whose bytes do not hash to the named digest -> {e.code}")
    text = (b"#include <vector>\n" * 4000)
    td = sha256_digest(text)
    cname = f"main/uploads/{uuid}/compressed-blobs/deflate/{td.hash}/{td.size_bytes}"
    comp = r._deflate(text)
    committed = cas.write(r.write_requests(cname, comp, 16384))
    assert committed == -1 and cas.has("main", td)
    print(f"compressed-blobs/deflate upload of {td.size_bytes} bytes as {len(comp)} bytes: committed_size={committed}")
    committed = cas.write(r.write_requests(cname.replace(uuid, "u2"), comp, 16384))
    assert committed == -1
    print(f"repeat compressed upload of an existing blob: committed_size={committed}")
    got = b"".join(cas.read(f"main/blobs/{d.hash}/{d.size_bytes}", read_offset=99_000, read_limit=500))
    assert got == data[99_000:99_500]
    print(f"Read blobs/... read_offset=99000 read_limit=500 -> {len(got)} bytes, match")
    got = b"".join(cas.read(f"main/compressed-blobs/deflate/{td.hash}/{td.size_bytes}"))
    assert r._inflate(got) == text
    print(f"Read compressed-blobs/deflate -> {len(got)} bytes, inflates to the {td.size_bytes}-byte blob")
    e = expect_error("INVALID_ARGUMENT", cas.read, f"main/compressed-blobs/deflate/{td.hash}/{td.size_bytes}",
                     read_limit=10)
    print(f"compressed read with read_limit=10 -> {e.code}")
    e = expect_error("INVALID_ARGUMENT", r.parse_resource_name, f"blobs/uploads/{uuid}/blobs/{d.hash}/1", True)
    print(f"instance name segment 'blobs' -> {e.code} (reserved keyword)")
    rn = r.parse_resource_name(f"uploads/{uuid}/blobs/blake3/{'ab' * 32}/7", True)
    print(f"blake3 upload names its function: function={rn['function']} instance={rn['instance']!r}")


# ---------------------------------------------------------------------------
# 6. Execute
# ---------------------------------------------------------------------------


def upper_tool(args, files):
    return 0, {args[2]: files[args[1]].upper()}, b""


def fail_tool(args, files):
    return 1, {}, b"error: deliberately failing\n"


def stage_action(cas, instance, args, outs, inputs, timeout_s=0, do_not_cache=False, salt=b""):
    cmd = r.enc_command(args, [("PATH", "/bin:/usr/bin")], outs)
    blobs = {sha256_digest(v): v for v in inputs.values()}
    root, dirs = r.merkle_root((p, sha256_digest(v), True) for p, v in inputs.items())
    blobs.update(dirs)
    blobs[sha256_digest(cmd)] = cmd
    action = r.enc_action(sha256_digest(cmd), root, timeout_s, do_not_cache, salt)
    ad = sha256_digest(action)
    blobs[ad] = action
    r.upload_blobs(cas, instance, blobs, batch_limit=4 << 20)
    return ad


def execute_flow() -> None:
    section("6. Execute: cache check, stages, skip_cache_lookup, do_not_cache, precondition failures")
    cas = r.Cas()
    ac = r.ActionCache(cas)
    ex = r.Executor(cas, ac, {"upper": upper_tool, "fail": fail_tool})
    inputs = {"in.txt": b"hello reapi\n"}
    ad = stage_action(cas, "", ["upper", "in.txt", "out.txt"], ["out.txt"], inputs)
    for label, skip in (("first Execute", False), ("second Execute", False), ("skip_cache_lookup", True)):
        stages, resp = ex.execute("", ad, skip_cache_lookup=skip)
        out = resp["result"]["outputs"][0]
        print(f"{label:18}: stages={'>'.join(stages)} cached_result={resp['cached_result']} "
              f"output={cas.blobs[('', out[1])].decode().strip()!r}")
    assert ex.executions == 2
    nc = stage_action(cas, "", ["upper", "in.txt", "out.txt"], ["out.txt"], inputs, do_not_cache=True)
    ex.execute("", nc)
    ex.execute("", nc)
    assert ("", nc) not in ac.entries
    print(f"do_not_cache=true: executed twice, AC entries for it: 0 (executions now {ex.executions})")
    fd = stage_action(cas, "", ["fail"], ["x"], inputs)
    _, resp = ex.execute("", fd)
    assert ("", fd) not in ac.entries
    print(f"exit_code={resp['result']['exit_code']}: result returned but not cached (status OK, exit code is not)")
    orphan = sha256_digest(b"never uploaded\n")
    cmd = r.enc_command(["upper", "gone.txt", "o"], [], ["o"])
    root, dirs = r.merkle_root([("gone.txt", orphan, True)])
    act = r.enc_action(sha256_digest(cmd), root)
    blobs = {sha256_digest(cmd): cmd, sha256_digest(act): act, **dirs}
    r.upload_blobs(cas, "", blobs, batch_limit=4 << 20)
    e = expect_error("FAILED_PRECONDITION", ex.execute, "", sha256_digest(act))
    print(f"input never uploaded -> {e.code}, PreconditionFailure MISSING subject={e.violations[0][:20]}.../16")


# ---------------------------------------------------------------------------
# 7. Output directories as Tree messages
# ---------------------------------------------------------------------------


def tree_encoding() -> None:
    section("7. Output directories: Tree message and the is_topologically_sorted record stream")
    outs = {"gen/a/x.h": b"x\n", "gen/b/x.h": b"x\n", "gen/b/deep/y.h": b"y\n", "gen/top.h": b"t\n"}
    rel = [(p[len("gen/"):], sha256_digest(v), False) for p, v in outs.items()]
    root, blobs = r.merkle_root(rel)
    root_blob, children = r.tree_records(root, blobs)
    full = r.enc_tree(root_blob, children)
    manual = r.key(1, 2) + r.varint(len(root_blob)) + root_blob
    manual += b"".join(r.key(2, 2) + r.varint(len(c)) + c for c in children)
    assert manual == full and full[0] == 0x0A and all(b == 0x12 for b in _record_tags(full)[1:])
    print(f"Tree for gen/: root + {len(children)} unique child Directory records, {len(full)} bytes, "
          f"record tags {' '.join(f'0x{t:02x}' for t in _record_tags(full))}")
    print(f"tree_digest={sha256_digest(full).short()}  root_directory_digest={root.short()}")


def _record_tags(buf: bytes) -> list[int]:
    tags, i = [], 0
    while i < len(buf):
        tags.append(buf[i])
        n, i = r._read_varint(buf, i + 1)
        i += n
    return tags


# ---------------------------------------------------------------------------
# 8. Build without the Bytes
# ---------------------------------------------------------------------------


def cc_tool(version: bytes):
    def tool(args, files):
        src = files[args[1]]
        return 0, {args[2]: stream(b"obj" + version + src, 200_000)}, b""
    return tool


def link_tool(args, files):
    objs = b"".join(files[p] for p in args[2:])
    return 0, {args[1]: stream(b"bin" + hashlib.sha256(objs).digest(), 600_000)}, b""


def bwob() -> None:
    section("8. Build without the Bytes: what each --remote_download_outputs mode fetches")
    cas = r.Cas()
    ac = r.ActionCache(cas, check_completeness=True)
    ex = r.Executor(cas, ac, {"cc": cc_tool(b"clang-21"), "ld": link_tool})
    srcs = {f"src/u{i}.cc": f"int u{i}() {{ return {i}; }}\n".encode() for i in range(4)}
    objs, results = {}, {}
    for p, s in srcs.items():
        o = p.replace("src/", "obj/").replace(".cc", ".o")
        ad = stage_action(cas, "", ["cc", p, o], [o], {p: s})
        _, resp = ex.execute("", ad)
        objs[o] = resp["result"]["outputs"][0][1]
        results[o] = ad
    # The link action's input root references the objects by digest only: they never leave the CAS.
    link_cmd = r.enc_command(["ld", "bin/app", *sorted(objs)], [], ["bin/app"])
    root, dirs = r.merkle_root((p, d, True) for p, d in objs.items())
    link_act = r.enc_action(sha256_digest(link_cmd), root)
    lad = sha256_digest(link_act)
    r.upload_blobs(cas, "", {sha256_digest(link_cmd): link_cmd, lad: link_act, **dirs}, batch_limit=4 << 20)
    _, resp = ex.execute("", lad)
    app = resp["result"]["outputs"][0][1]
    total = sum(d.size_bytes for d in objs.values()) + app.size_bytes
    modes = {"all": total, "toplevel": app.size_bytes, "minimal": 0}
    for mode, n in modes.items():
        print(f"--remote_download_outputs={mode:8}: downloads {n:>9,} of {total:,} output bytes")
    # A local action (for example a no-remote-exec packaging step) needs bin/app under minimal.
    print(f"minimal + one local action consuming bin/app: downloads {app.size_bytes:,} bytes (inputs of local actions)")
    # Eviction: an object file disappears; the next build that needs it must recover.
    victim = "obj/u2.o"
    cas.evict("", objs[victim])
    e = expect_error("NOT_FOUND", ac.get, "", results[victim])
    print(f"evict {victim} from CAS: completeness-checking GetActionResult -> {e.code}")
    relink = r.enc_command(["ld", "bin/app2", *sorted(objs)], [], ["bin/app2"])
    rl_act = r.enc_action(sha256_digest(relink), root)
    r.upload_blobs(cas, "", {sha256_digest(relink): relink, sha256_digest(rl_act): rl_act}, batch_limit=4 << 20)
    e = expect_error("FAILED_PRECONDITION", ex.execute, "", sha256_digest(rl_act))
    print(f"new link action over the evicted object -> {e.code} with {len(e.violations)} MISSING violation")
    _, resp = ex.execute("", results[victim])  # rewinding: re-run the producer, which re-uploads
    assert not resp["cached_result"] and resp["result"]["outputs"][0][1] == objs[victim]
    _, resp = ex.execute("", sha256_digest(rl_act))
    print(f"rewind: re-execute the compile of {victim} (same digest regenerated), then the link succeeds: "
          f"exit_code={resp['result']['exit_code']}")


# ---------------------------------------------------------------------------
# 9. Cache poisoning
# ---------------------------------------------------------------------------


def poisoning() -> None:
    section("9. Action cache poisoning: an undeclared host tool and an open AC")
    src = {"src/main.cc": b"int main() { return 0; }\n"}
    cas = r.Cas()
    open_ac = r.ActionCache(cas)
    ci = r.Executor(cas, open_ac, {"cc": cc_tool(b"clang-21")}, worker="ci-worker")
    ad = stage_action(cas, "", ["cc", "src/main.cc", "obj/main.o"], ["obj/main.o"], src)
    # A laptop with a different host compiler runs the same action locally and uploads its result.
    laptop_code, laptop_out, _ = cc_tool(b"clang-17-patched")(["cc", "src/main.cc", "obj/main.o"],
                                                            dict(src))
    lo = laptop_out["obj/main.o"]
    cas._store("", sha256_digest(lo), lo)
    open_ac.update("laptop", "", ad, r.enc_action_result([("obj/main.o", sha256_digest(lo), True)],
                                                        0, r.EMPTY, r.EMPTY, "laptop"))
    _, resp = ci.execute("", ad)
    served = resp["result"]["outputs"][0][1]
    honest = sha256_digest(cc_tool(b"clang-21")(["cc", "src/main.cc", "obj/main.o"], dict(src))[1]["obj/main.o"])
    assert resp["cached_result"] and served != honest
    print(f"same action digest {ad.short()} on both machines: compiler is not an input")
    print(f"open AC: laptop wrote first; CI gets cached_result=True, object {served.short()} "
          f"!= CI compiler's {honest.short()}")
    statuses = cas.batch_update("", [(honest, lo)])
    assert statuses == ["INVALID_ARGUMENT"]
    print(f"CAS cannot be poisoned the same way: storing laptop bytes under the honest digest -> {statuses[0]}")
    # Mitigation 1: only the remote execution workers (and trusted CI) write the AC.
    cas2 = r.Cas()
    acl_ac = r.ActionCache(cas2, writers={"ci-worker"})
    ad2 = stage_action(cas2, "", ["cc", "src/main.cc", "obj/main.o"], ["obj/main.o"], src)
    e = expect_error("PERMISSION_DENIED", acl_ac.update, "laptop", "", ad2, b"")
    print(f"AC with writer ACL {{ci-worker}}: laptop UpdateActionResult -> {e.code}")
    # Mitigation 2: declare the toolchain as an input, so different compilers mean different keys.
    keys = set()
    for ver in (b"clang-21", b"clang-17-patched"):
        tc = {**src, "toolchain/bin/cc": b"compiler binary " + ver}
        keys.add(stage_action(cas2, "", ["toolchain/bin/cc", "src/main.cc", "obj/main.o"], ["obj/main.o"], tc))
    assert len(keys) == 2
    print(f"compiler declared in the input root: 2 compilers -> {len(keys)} distinct action digests")
    # Mitigation 3: rotate Action.salt to disown every entry written under the old salt.
    old = ad
    new = stage_action(cas, "", ["cc", "src/main.cc", "obj/main.o"], ["obj/main.o"], src, salt=b"2026-11-rotation")
    _, resp_new = ci.execute("", new)
    assert old != new and not resp_new["cached_result"] and resp_new["result"]["outputs"][0][1] == honest
    print(f"salt rotated: new digest {new.short()}, cache miss, CI rebuilds the honest object")
    # Mitigation 4: skip_cache_lookup re-executes and overwrites the poisoned entry in place.
    ci.execute("", old, skip_cache_lookup=True)
    _, resp = ci.execute("", old)
    assert resp["cached_result"] and resp["result"]["outputs"][0][1] == honest
    print("skip_cache_lookup on the old digest: entry overwritten, next lookup serves the honest object")
    assert laptop_code == 0


# ---------------------------------------------------------------------------
# 10. Digest functions
# ---------------------------------------------------------------------------


def digest_functions() -> None:
    section("10. Digest functions: SHA256TREE test vectors and GITSHA1")
    vectors = [line.split() for line in (HERE / "fixtures/sha256tree_test_vectors.txt").read_text().splitlines()
               if line and not line.startswith("#")]
    seq = bytes(i % 251 for i in range(max(int(n) for n, _ in vectors)))
    ok = sum(r.sha256tree(seq[:int(n)]).hex() == h for n, h in vectors)
    assert ok == len(vectors)
    small_ok = all(r.sha256tree(seq[:n]) == hashlib.sha256(seq[:n]).digest() for n in (0, 1, 1023, 1024))
    assert small_ok
    plain = hashlib.sha256(seq[:2048]).hexdigest()
    print(f"SHA256TREE: {ok}/{len(vectors)} spec test vectors match (1025..102400 bytes); "
          f"<=1024 bytes equals plain SHA-256: {small_ok}")
    print(f"2048-byte input: SHA256TREE {r.sha256tree(seq[:2048]).hex()[:16]} vs SHA-256 {plain[:16]}")
    assert r.gitsha1(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    print(f"GITSHA1 of the empty blob = {r.gitsha1(b'')} (Git's empty blob name)")


# ---------------------------------------------------------------------------
# 11. Content-defined chunking
# ---------------------------------------------------------------------------

GEAR = [int.from_bytes(hashlib.md5(bytes([i])).digest()[:8], "big") for i in range(256)]


def gear_chunks(data: bytes, avg: int) -> list[bytes]:
    """A plain gear-hash chunker using the spec's GEAR table; NOT a conformant FastCDC 2020
    (no normalization levels or the reference MASKS table): it only shows why CDC dedups."""
    mask = (avg - 1) << (64 - avg.bit_length() + 1)
    lo, hi = avg // 4, avg * 4
    chunks, start, h = [], 0, 0
    for i, b in enumerate(data):
        h = ((h << 1) + GEAR[b]) & 0xFFFFFFFFFFFFFFFF
        n = i - start + 1
        if (n >= lo and not h & mask) or n >= hi:
            chunks.append(data[start:i + 1])
            start, h = i + 1, 0
    if start < len(data):
        chunks.append(data[start:])
    return chunks


def chunking() -> None:
    section("11. Chunked CAS (GetChunkMapping / RegisterChunkMapping): why content-defined cuts")
    assert GEAR[0] == int(hashlib.md5(b"\x00").hexdigest()[:16], 16)
    base = stream(b"artifact", 1 << 20)
    edited = b"NEW HEADER" + base
    reuse = {}
    for label, split in (("fixed 64 KiB", lambda d: [d[i:i + 65536] for i in range(0, len(d), 65536)]),
                         ("gear CDC avg 64 KiB", lambda d: gear_chunks(d, 65536))):
        a, b = split(base), split(edited)
        have = {sha256_digest(c) for c in a}
        reused = [c for c in b if sha256_digest(c) in have]
        assert b"".join(b) == edited
        reuse[label] = len(reused)
        print(f"{label:20}: {len(b)} chunks after a 10-byte prepend, {len(reused)} already in CAS, "
              f"{sum(map(len, b)) - sum(map(len, reused)):,} bytes to upload")
    # Fixed-size cuts all shift after a prepend; content-defined cuts resynchronise.
    assert reuse["fixed 64 KiB"] == 0
    assert reuse["gear CDC avg 64 KiB"] >= 10


def main() -> int:
    bazel_replay()
    canonical_form()
    merkle_incremental()
    cas_batching()
    bytestream()
    execute_flow()
    tree_encoding()
    bwob()
    poisoning()
    digest_functions()
    chunking()
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
