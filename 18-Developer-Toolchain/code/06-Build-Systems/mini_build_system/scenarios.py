"""Drive minibuild through the scenarios the note walks through.

Every scenario prints derived facts only (key lists, counts, short digests),
so the output does not depend on paths, PIDs, timing, or thread interleaving.
"Matches clean build" rebuilds the same sources in a fresh workspace with an
empty cache and compares the final digest: the correctness oracle from
Definition 3.1 of Build Systems a la Carte, applied after every scenario.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import depfile  # noqa: E402
import minibuild as mb  # noqa: E402
import rules  # noqa: E402

PY = sys.executable
TOOLS = {"toycc": os.path.join(HERE, "toycc.py")}
BASE = tempfile.mkdtemp(prefix="minibuild-lab-")
CLOCK = [1577836800]  # 2020-01-01T00:00:00Z: sources get fixed, increasing mtimes

SOURCES = {
    "src/main.c": '#include "util.h"\n#include "legacy.h"\nint main(void) { return util(); } // entry\n',
    "src/util.c": '#include "util.h"\nint util(void) { return 42; }\n',
    "include/util.h": '#include "build config.h"\nint util(void);\n',
    "include/build config.h": "#define LEVEL 1\n",
    "include/legacy.h": "#define LEGACY 1\n",
    "app.list": "out/main.o out/util.o\n",
    "stamp.txt": "v1\n",
}

MAKEFILE = (
    "mk/util.o: src/util.c\n"
    "\t@mkdir -p mk\n"
    f"\t@{PY} {TOOLS['toycc']} compile src/util.c -o mk/util.o -MF mk/util.d -I include\n"
)


def short(d: str) -> str:
    return d[:12]


def write(ws: str, rel: str, text: str, mtime: int | None = None) -> None:
    path = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        f.write(text)
    if mtime is None:
        CLOCK[0] += 10
        mtime = CLOCK[0]
    os.utime(path, (mtime, mtime))


def read(ws: str, rel: str) -> str:
    with open(os.path.join(ws, rel), encoding="ascii") as f:
        return f.read()


def new_workspace(name: str, files: dict[str, str] | None = None) -> str:
    ws = os.path.join(BASE, name)
    os.makedirs(ws)
    for rel, text in (files or SOURCES).items():
        write(ws, rel, text)
    return ws


def engine(ws: str, cache: str, rule_set=None, env=None, jobs=4, stat=mb.STRONG_STAT) -> mb.Engine:
    # Each stat-key shape gets its own cache file, so scenario 8 compares like with like.
    return mb.Engine(ws, rule_set or rules.project(), cache, TOOLS, env=env, jobs=jobs,
                     stat_fields=stat, python=PY, stat_name="stat-" + "-".join(f[3:] for f in stat))


def source_files(ws: str) -> dict[str, str]:
    out = {}
    for rel in SOURCES:
        if os.path.exists(os.path.join(ws, rel)):
            out[rel] = read(ws, rel)
    for d in ("src", "include"):
        for name in sorted(os.listdir(os.path.join(ws, d))):
            out.setdefault(f"{d}/{name}", read(ws, f"{d}/{name}"))
    return out


ORACLE_N = [0]


def oracle(ws: str, target: str, env=None) -> str:
    """Digest of `target` from a from-scratch build of the same sources."""
    ORACLE_N[0] += 1
    name = f"oracle{ORACLE_N[0]}"
    clean = new_workspace(name, source_files(ws))
    engine(clean, os.path.join(BASE, name + "-cache"), env=env).build([target])
    return mb.sha256_bytes(open(os.path.join(clean, target), "rb").read())


def show(title: str, st: mb.Stats, ws: str, target="out/app", env=None, hashed: int | None = None) -> None:
    print(f"== {title} ==")
    print("executed: " + (" ".join(st.executed) or "-"))
    print("verified: " + (" ".join(st.verified) or "-"))
    print("restored: " + (" ".join(st.restored) or "-"))
    if st.unchanged:
        print("executed but result unchanged (early cutoff point): " + " ".join(st.unchanged))
    if st.corrupt:
        print(f"corrupt CAS blobs detected: {st.corrupt}")
    if hashed is not None:
        print(f"files hashed: {hashed}")
    got = mb.sha256_bytes(open(os.path.join(ws, target), "rb").read())
    print(f"{target} digest: {short(got)}")
    print("matches clean build: " + ("yes" if got == oracle(ws, target, env) else "NO (stale output)"))
    print()


def make_q(ws: str) -> str:
    r = subprocess.run(["make", "-q", "mk/util.o"], cwd=ws, capture_output=True, text=True, check=False)
    return {0: "up to date", 1: "needs rebuild"}.get(r.returncode, f"error {r.returncode}")


def build(e: mb.Engine, targets=("out/app",)) -> tuple[mb.Stats, int]:
    st = e.build(list(targets))
    return st, e.hasher.hashed


def main() -> None:
    cache = os.path.join(BASE, "cache")
    ws = new_workspace("ws")
    write(ws, "Makefile", MAKEFILE)

    st, h = build(engine(ws, cache))
    show("1. Clean build", st, ws, hashed=h)
    subprocess.run(["make", "-s", "mk/util.o"], cwd=ws, check=True)

    tr = engine(ws, cache).db.traces["out/main.o"][0]
    print("== 2. The constructive trace recorded for out/main.o ==")
    for n, group in enumerate(tr["deps"]):
        print(f"group {n}: " + ", ".join(f"{k}={short(v)}" for k, v in group))
    print(f"result: {short(tr['result'])}")
    print()

    st, h = build(engine(ws, cache))
    show("3. No-op build", st, ws, hashed=h)

    # touch: same bytes, new mtime and ctime; the mtime is set 10 s past mk/util.o so
    # that a Make with one-second timestamps also sees the source as newer.
    t = os.stat(os.path.join(ws, "mk/util.o")).st_mtime_ns + 10 * 10**9
    os.utime(os.path.join(ws, "src/util.c"), ns=(t, t))
    print(f"(make -q mk/util.o after the touch: {make_q(ws)})")
    st, h = build(engine(ws, cache))
    show("4. touch src/util.c (bytes unchanged)", st, ws, hashed=h)

    write(ws, "src/main.c", read(ws, "src/main.c").replace("// entry", "// program entry point"))
    st, h = build(engine(ws, cache))
    show("5. Comment-only edit in src/main.c", st, ws, hashed=h)

    write(ws, "include/build config.h", "#define LEVEL 2\n")
    st, h = build(engine(ws, cache))
    show("6. Edit 'include/build config.h' (reached only through the depfile)", st, ws, hashed=h)

    write(ws, "src/extra.c", "int extra(void) { return 7; }\n")
    write(ws, "app.list", "out/main.o out/util.o out/extra.o\n")
    st, h = build(engine(ws, cache))
    show("7. Add out/extra.o to app.list (a dependency read from a file)", st, ws, hashed=h)

    CLOCK[0] += 10  # undo scenario 4's future mtime so that make's target ends up newer
    os.utime(os.path.join(ws, "src/util.c"), (CLOCK[0], CLOCK[0]))
    subprocess.run(["make", "-s", "mk/util.o"], cwd=ws, check=True)
    weak = mb.WEAK_STAT
    strong = mb.WEAK_STAT + ("st_ctime_ns", "st_ino", "st_dev", "st_mode")
    build(engine(ws, cache, stat=weak))  # warm both stat caches before the edit
    build(engine(ws, cache, stat=strong))
    p = os.path.join(ws, "src/util.c")
    old = os.stat(p)
    new = read(ws, "src/util.c").replace("42", "43")
    with open(p, "w", encoding="ascii") as f:
        f.write(new)
    os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns))  # what cp -p, rsync -t, or tar x do
    print("== 8. Same-size edit of src/util.c with its old mtime restored ==")
    print(f"make -q mk/util.o: {make_q(ws)}")
    st, h = build(engine(ws, cache, stat=weak))
    print(f"stat cache keyed on (size, mtime): files hashed {h}, executed: " + (" ".join(st.executed) or "-"))
    got = mb.sha256_bytes(open(os.path.join(ws, "out/app"), "rb").read())
    print("  matches clean build: " + ("yes" if got == oracle(ws, "out/app") else "NO (stale output)"))
    st, h = build(engine(ws, cache, stat=strong))
    print(f"stat cache keyed on (size, mtime, ctime, inode, dev, mode): files hashed {h}, executed: "
          + " ".join(st.executed))
    got = mb.sha256_bytes(open(os.path.join(ws, "out/app"), "rb").read())
    print("  matches clean build: " + ("yes" if got == oracle(ws, "out/app") else "NO (stale output)"))
    print()

    shutil.rmtree(os.path.join(ws, "out"))
    st, h = build(engine(ws, cache))
    show("9. rm -rf out/ (constructive traces restore from the CAS)", st, ws, hashed=h)

    ws2 = new_workspace("elsewhere/ws2", source_files(ws))
    st, h = build(engine(ws2, cache))
    show("10. Second workspace at another path, same shared cache", st, ws2, hashed=h)

    e = engine(ws, cache)
    digest = e.db.traces["out/main.o"][0]["result"]
    with open(e.cas.path(digest), "r+b") as f:
        f.write(b"X")  # flip the first byte of the stored object
    os.remove(os.path.join(ws, "out/main.o"))
    st, h = build(e)
    show("11. Corrupt the CAS blob of out/main.o, then delete out/main.o", st, ws, hashed=h)

    st, h = build(engine(ws, cache, rule_set=rules.project(compile_fn=rules.cc_v2)))
    show("12. Change the compile recipe (add -I src); outputs stay identical", st, ws, hashed=h)

    print("== 13. Untracked environment variable ==")
    wl = new_workspace("leaky")
    lcache = os.path.join(BASE, "leaky-cache")
    leaky = rules.project(compile_fn=rules.cc_leaky)
    build(engine(wl, lcache, rule_set=leaky, env={"TOYCC_OPT": "-O0"}))
    st, _ = build(engine(wl, lcache, rule_set=leaky, env={"TOYCC_OPT": "-O3"}))
    print("leaky rule, TOYCC_OPT -O0 then -O3, executed: " + (" ".join(st.executed) or "-"))
    got = mb.sha256_bytes(open(os.path.join(wl, "out/app"), "rb").read())
    ok = got == oracle(wl, "out/app", env={"TOYCC_OPT": "-O3"})
    print("  matches clean build: " + ("yes" if ok else "NO (stale output)"))
    st, _ = build(engine(wl, lcache, env={"TOYCC_OPT": "-O3"}))
    print("declared rule (needs env:TOYCC_OPT), executed: " + " ".join(st.executed))
    got = mb.sha256_bytes(open(os.path.join(wl, "out/app"), "rb").read())
    ok = got == oracle(wl, "out/app", env={"TOYCC_OPT": "-O3"})
    print("  matches clean build: " + ("yes" if ok else "NO (stale output)"))
    print()

    print("== 14. A non-deterministic task defeats early cutoff ==")
    stamp_files = dict(SOURCES, **{"app.list": "out/main.o out/util.o out/stamp.o\n"})
    wsd = new_workspace("stamp", stamp_files)
    dcache = os.path.join(BASE, "stamp-cache")
    build(engine(wsd, dcache))
    write(wsd, "stamp.txt", "v2\n")
    st, _ = build(engine(wsd, dcache))
    print("wall-clock stamp, stamp.txt edited, executed: " + " ".join(st.executed))
    print("  unchanged results: " + (" ".join(st.unchanged) or "-"))
    sde = {"SOURCE_DATE_EPOCH": "1700000000"}
    build(engine(wsd, dcache, env=sde))
    write(wsd, "stamp.txt", "v3\n")
    st, _ = build(engine(wsd, dcache, env=sde))
    print("SOURCE_DATE_EPOCH set, stamp.txt edited, executed: " + " ".join(st.executed))
    print("  unchanged results: " + (" ".join(st.unchanged) or "-"))
    print()

    print("== 15. A header changes while the compile that read it is running ==")
    wr = new_workspace("race")
    rcache = os.path.join(BASE, "race-cache")
    try:
        build(engine(wr, rcache, rule_set=rules.project(edit_during_for="out/util.o")), targets=("out/util.o",))
        print("build result: succeeded (the race was NOT detected)")
    except mb.BuildError as err:
        print(f"build result: failed: {err}")
    st, h = build(engine(wr, rcache))
    show("15b. Rebuild after the race", st, wr, hashed=h)

    write(ws, "src/main.c", read(ws, "src/main.c").replace('#include "legacy.h"\n', ""))
    os.remove(os.path.join(ws, "include/legacy.h"))
    st, h = build(engine(ws, cache))
    show("16. Remove an #include and delete legacy.h in the same change", st, ws, hashed=h)

    print("== 17. A dependency cycle ==")
    try:
        build(engine(ws, cache), targets=("out/a.txt",))
        print("build result: succeeded (cycle NOT detected)")
    except mb.CycleError as err:
        print(f"build result: {err}")
    print()

    print("== 18. Suspending versus restarting scheduler, clean build of 3 objects ==")
    wsr = new_workspace("sched", dict(SOURCES, **{"app.list": "out/main.o out/util.o out/extra.o\n",
                                                  "src/extra.c": "int extra(void) { return 7; }\n"}))
    st, _ = build(engine(wsr, os.path.join(BASE, "sched-cache")))
    print(f"suspending: task starts {st.starts}, actions {len(st.executed)}")
    wsr2 = new_workspace("sched2", source_files(wsr))
    starts, actions = mb.restarting_build(wsr2, rules.project(), TOOLS, "out/app", PY)
    print(f"restarting: task starts {starts}, actions {actions}")
    same = read(wsr, "out/app") == read(wsr2, "out/app")
    print("same out/app: " + ("yes" if same else "no"))
    print()

    print("== 19. Depfile parsing (GCC escaping, as Ninja documents it) ==")
    cases = [
        "out/a.o: src/a.c include/build\\ config.h\n",
        "out/a.o: src/a.c \\\n  x$$y.h c\\#d.h\n",
        "out/a.o: back\\\\\\ slash.h two\\\\ next.h\n",
        "out/a.o: a.h\na.h:\n",
        "no colon here\n",
    ]
    for text in cases:
        try:
            parsed = depfile.parse(text)
            print(f"{text!r} -> {parsed}")
        except depfile.DepfileError as err:
            print(f"{text!r} -> error: {err}")
    for name in ("build config.h", "x$y.h", "c#d.h", "back\\ slash.h"):
        rt = depfile.prerequisites_of("t: " + depfile.escape(name) + "\n", "t")
        print(f"round trip {name!r}: " + ("ok" if rt == [name] else f"FAILED {rt}"))

    shutil.rmtree(BASE)


if __name__ == "__main__":
    main()
