"""Build one small C and Python project twice under different conditions, then remove the
differences one fix at a time, explaining each remaining difference like a tiny diffoscope.

Build A and build B differ the way Debian's reproducibility CI varies its two builds:
build directory, wall clock (B starts in a later second), source mtimes (a fresh checkout),
TZ (Etc/GMT+12 versus Etc/GMT-14), locale (en_US.UTF-8 versus et_EE.UTF-8), PYTHONHASHSEED,
and umask (022 versus 002). The fixes are applied cumulatively, so each stage shows what the
previous fixes did not cover.

Every check can fail: a stage reports an artifact as identical only if its SHA-256 matches,
and each reason is printed only when the bytes behind it really differ.
Output contains no paths, times, or hashes, so it is the same on every run of one platform.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE / "project"
CC = os.environ.get("CC", "cc")
DARWIN = platform.system() == "Darwin"
SOURCE_DATE_EPOCH = 1700000000  # "time of the last commit"; 2023-11-14T22:13:20Z
LIB_SOURCES = ["seed", "stamp", "table", "zone"]
ARTIFACTS = [
    "gen/manifest.h",
    "obj/main.o",
    "obj/seed.o",
    "obj/stamp.o",
    "obj/table.o",
    "obj/zone.o",
    "out/libapp.a",
    "out/app",
    "out/version.pyc",
    "out/bundle.tar.gz",
]
VARIANTS = {
    "A": {"dir": "alpha", "TZ": "Etc/GMT+12", "LC_ALL": "en_US.UTF-8", "PYTHONHASHSEED": "1",
          "umask": 0o022, "src_mtime": 1710000000},
    "B": {"dir": "bravo-second-builder", "TZ": "Etc/GMT-14", "LC_ALL": "et_EE.UTF-8", "PYTHONHASHSEED": "2",
          "umask": 0o002, "src_mtime": 1760000000},
}
STAGES = [
    ("naive", "no fixes"),
    ("sde", "export SOURCE_DATE_EPOCH (compiler dates, generator date, hash-based .pyc)"),
    ("tz", "export TZ=UTC0"),
    ("locale", "export LC_ALL=C (object list order)"),
    ("sort", "sorted() in the generator (set order)"),
    ("prefix", "path maps: -ffile-prefix-map, -oso_prefix on macOS, py_compile dfile"),
    ("ar", "deterministic ar (ZERO_AR_DATE=1 on macOS, the D modifier elsewhere)"),
    ("umask", "umask 022 for every build (Apple ar and libtool -D copy each member's mode)"),
    ("bundle", "normalized tarball: sorted names, clamped mtimes, owner 0, fixed modes, gzip mtime 0"),
]
DATE_RE = re.compile(rb"[A-Z][a-z]{2} [ 0-9][0-9] [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def run(cmd: list[str], cwd: Path, env: dict[str, str], stdin: str | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit("command failed (%d): %s\n%s" % (proc.returncode, " ".join(cmd), proc.stderr))
    return proc.stdout


def build(root: Path, name: str, fixes: set[str]) -> None:
    var = VARIANTS[name]
    shutil.copytree(PROJECT, root)
    for sub in ("gen", "obj", "out"):
        (root / sub).mkdir()
    # A checkout writes source files with the checkout time, not the commit time.
    for i, path in enumerate(sorted(p for p in root.rglob("*") if p.is_file())):
        os.utime(path, (var["src_mtime"] + i, var["src_mtime"] + i))

    env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    env["TZ"] = "UTC0" if "tz" in fixes else var["TZ"]
    env["LC_ALL"] = "C" if "locale" in fixes else var["LC_ALL"]
    env["PYTHONHASHSEED"] = var["PYTHONHASHSEED"]
    if "sde" in fixes:
        env["SOURCE_DATE_EPOCH"] = str(SOURCE_DATE_EPOCH)
    if "sort" in fixes:
        env["REPRO_FIX_SORT"] = "1"
    if "ar" in fixes and DARWIN:
        env["ZERO_AR_DATE"] = "1"

    old_umask = os.umask(0o022 if "umask" in fixes else var["umask"])
    try:
        manifest = run([sys.executable, "gen_manifest.py"], root, env)
        (root / "gen" / "manifest.h").write_text(manifest)

        cflags = ["-g", "-O1", "-I", str(root / "gen")]
        if "prefix" in fixes:
            # Map both spellings: with no PWD in the environment the compiler records getcwd(),
            # which on macOS resolves /var to /private/var, and a prefix map is a textual match.
            for form in sorted({str(root), os.path.realpath(root)}):
                cflags.append("-ffile-prefix-map=%s=." % form)
        for src in ["main"] + LIB_SOURCES:
            run([CC, *cflags, "-c", str(root / "src" / (src + ".c")), "-o", "obj/%s.o" % src], root, env)

        # The object list comes from `ls | sort`, as a Makefile $(shell ...) would build it.
        objs = run(["sort"], root, env, stdin="".join(s + ".o\n" for s in LIB_SOURCES)).split()
        objs = ["obj/" + o for o in objs]
        arflags = "rcsD" if ("ar" in fixes and not DARWIN) else "rcs"
        run(["ar", arflags, "out/libapp.a", *objs], root, env)

        ldflags = ["-Wl,-oso_prefix,."] if ("prefix" in fixes and DARWIN) else []
        run([CC, "-g", *ldflags, "-o", "out/app", "obj/main.o", *objs], root, env)

        dfile = "'py/version.py'" if "prefix" in fixes else "None"
        run([sys.executable, "-c",
             "import py_compile; py_compile.compile(%r, cfile='out/version.pyc', dfile=%s, doraise=True)"
             % (str(root / "py" / "version.py"), dfile)], root, env)

        write_bundle(root, normalized="bundle" in fixes)
    finally:
        os.umask(old_umask)


def write_bundle(root: Path, normalized: bool) -> None:
    members = [("app", root / "out" / "app"), ("libapp.a", root / "out" / "libapp.a"),
               ("version.pyc", root / "out" / "version.pyc"), ("manifest.h", root / "gen" / "manifest.h")]
    target = root / "out" / "bundle.tar.gz"
    with open(target, "wb") as raw:
        if normalized:
            gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
            members.sort(key=lambda m: m[0].encode())
        else:
            gz = gzip.GzipFile(mode="wb", fileobj=raw)  # header gets the current time
            members.sort(key=lambda m: os.stat(m[1]).st_mtime_ns)  # "whatever order the tool walked"
        with gz, tarfile.open(fileobj=gz, mode="w") as tar:
            for arcname, path in members:
                info = tar.gettarinfo(str(path), arcname)
                if normalized:
                    info.mtime = min(int(info.mtime), SOURCE_DATE_EPOCH)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o755 if info.mode & 0o100 else 0o644
                with open(path, "rb") as f:
                    tar.addfile(info, f)


def ar_members(data: bytes) -> list[tuple[str, bytes, bytes, bytes, bytes, bytes]]:
    """Parse a common or BSD 4.4 ar archive into (name, mtime, uid, gid, mode, body)."""
    assert data[:8] == b"!<arch>\n", "not an ar archive"
    out, pos = [], 8
    while pos + 60 <= len(data):
        hdr = data[pos:pos + 60]
        name, size = hdr[0:16].decode().strip(), int(hdr[48:58])
        body = data[pos + 60:pos + 60 + size]
        if name.startswith("#1/"):  # BSD long name stored at the start of the body
            n = int(name[3:])
            name, body = body[:n].rstrip(b"\0").decode(), body[n:]
        out.append((name, hdr[16:28], hdr[28:34], hdr[34:40], hdr[40:48], body))
        pos += 60 + size + (size & 1)
    return out


def nm_lines(path: Path, args: list[str]) -> list[str]:
    return subprocess.run(["nm", *args, str(path)], capture_output=True, text=True, check=True).stdout.splitlines()


def explain(rel: str, a: bytes, b: bytes, roots: tuple[Path, Path], pa: Path, pb: Path, fixes: set[str]) -> list[str]:
    why: list[str] = []
    leaks = [r for r in roots for form in {str(r), os.path.realpath(r)} if form.encode() in (a + b)]
    if leaks:
        why.append("embeds the build directory")
    if set(DATE_RE.findall(a)) != set(DATE_RE.findall(b)):
        why.append("embeds __DATE__ __TIME__")
    if rel.endswith("manifest.h"):
        la, lb = a.decode().splitlines(), b.decode().splitlines()
        if la[0] != lb[0]:
            why.append("FEATURES order differs (set iteration under PYTHONHASHSEED)")
        if la[1] != lb[1]:
            if "sde" in fixes:  # both values are fixed by SOURCE_DATE_EPOCH, so printing them is stable
                why.append("GENERATED_ON differs: A %s | B %s" % (la[1].split('"')[1], lb[1].split('"')[1]))
            else:
                why.append("GENERATED_ON differs (wall clock and TZ)")
    elif rel.endswith(".pyc"):
        fa, fb = int.from_bytes(a[4:8], "little"), int.from_bytes(b[4:8], "little")
        if fa == fb == 0 and a[8:12] != b[8:12]:
            why.append("pyc header records the source mtime (flags=0, timestamp mode)")
    elif rel.endswith(".a"):
        ma, mb = ar_members(a), ar_members(b)
        if [m[0] for m in ma] != [m[0] for m in mb]:
            why.append("member order differs")
        for field, label in ((1, "mtime"), (2, "uid"), (3, "gid"), (4, "mode")):
            if any(x[field] != y[field] for x, y in zip(ma, mb)):
                why.append("member %s differs" % label)
    elif rel.endswith(".tar.gz"):
        if a[4:8] != b[4:8]:
            why.append("gzip header mtime differs")
        ta, tb = tarfile.open(fileobj=io.BytesIO(a)), tarfile.open(fileobj=io.BytesIO(b))
        ia, ib = ta.getmembers(), tb.getmembers()
        if [m.name for m in ia] != [m.name for m in ib]:
            why.append("tar member order differs")
        da, db = {m.name: m for m in ia}, {m.name: m for m in ib}
        for attr in ("mtime", "mode", "uid", "gid", "uname"):
            if any(getattr(da[n], attr) != getattr(db[n], attr) for n in da):
                why.append("tar member %s differs" % attr)
        changed = [n for n in sorted(da) if ta.extractfile(da[n]).read() != tb.extractfile(db[n]).read()]
        if changed:
            why.append("contents differ: " + ", ".join(changed))
    elif rel.endswith("/app"):
        order = [[ln.split()[-1] for ln in nm_lines(p, ["-n"]) if ln.split()[-1].endswith("_value")] for p in (pa, pb)]
        if order[0] != order[1]:
            why.append("function layout differs (link order)")
        if DARWIN:
            osa, osb = ([ln.split()[0] for ln in nm_lines(p, ["-ap"]) if " OSO " in ln] for p in (pa, pb))
            if list(osa) != list(osb):
                why.append("debug map N_OSO entries record object mtimes")
    if not why:
        why.append("other bytes differ")
    return why


def wait_for_next_second() -> None:
    now = time.time()
    time.sleep(math.ceil(now) - now + 0.05)


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="repro-"))
    try:
        fixes: set[str] = set()
        for index, (key, title) in enumerate(STAGES):
            if key != "naive":
                fixes.add(key)
            stage_dir = work / ("stage%d" % index)
            roots = (stage_dir / VARIANTS["A"]["dir"] / "proj", stage_dir / VARIANTS["B"]["dir"] / "proj")
            build(roots[0], "A", fixes)
            wait_for_next_second()  # build B runs in a later wall-clock second
            build(roots[1], "B", fixes)
            diffs = []
            for rel in ARTIFACTS:
                pa, pb = roots[0] / rel, roots[1] / rel
                a, b = pa.read_bytes(), pb.read_bytes()
                if hashlib.sha256(a).digest() != hashlib.sha256(b).digest():
                    diffs.append((rel, explain(rel, a, b, roots, pa, pb, fixes)))
            print("stage %d: +%s" % (index, title) if index else "stage 0: naive build, no fixes")
            print("  identical: %d of %d artifacts" % (len(ARTIFACTS) - len(diffs), len(ARTIFACTS)))
            for rel, why in diffs:
                print("  %-18s %s" % (rel, "; ".join(why)))
            if index == len(STAGES) - 1:
                report_final(roots[0], roots[1], diffs)
            else:
                print()
    finally:
        shutil.rmtree(work)


def report_final(ra: Path, rb: Path, diffs: list) -> None:
    if diffs:
        sys.exit("FAIL: the fully fixed builds still differ")
    print()
    print("Final artifacts are bit-for-bit identical across both builds.")
    app = subprocess.run([str(ra / "out" / "app")], capture_output=True, text=True, check=True).stdout
    for line in app.splitlines():
        print("  app prints   " + line)
    pyc = (ra / "out" / "version.pyc").read_bytes()
    print("  version.pyc  flags word = %d (1 = hash-based, 2 = check_source)" % int.from_bytes(pyc[4:8], "little"))
    gz = (ra / "out" / "bundle.tar.gz").read_bytes()
    print("  bundle       gzip mtime field = %d" % int.from_bytes(gz[4:8], "little"))
    with tarfile.open(ra / "out" / "bundle.tar.gz") as tar:
        for m in tar.getmembers():
            print("  bundle       %-12s mode %o uid %d gid %d mtime %d" % (m.name, m.mode, m.uid, m.gid, m.mtime))
    for name, mtime, uid, gid, mode, _ in ar_members((ra / "out" / "libapp.a").read_bytes()):
        print("  libapp.a     %-18s mtime %s uid %s gid %s mode %s"
              % (name, mtime.decode().strip(), uid.decode().strip(), gid.decode().strip(), mode.decode().strip()))


if __name__ == "__main__":
    main()
