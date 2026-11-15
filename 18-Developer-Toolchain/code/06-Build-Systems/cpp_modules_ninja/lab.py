#!/usr/bin/env python3
"""Drive a C++20 named-modules build through Ninja dyndep by hand, then through CMake.

Usage: lab.py <workdir>   (environment: CXX, SCAN_DEPS, NINJA, CMAKE, LAB_DIR, optional GXX)

Part 1 writes a build.ninja with the shape CMake generates (scan -> collate -> compile with
dyndep -> link), runs it, and edits sources to show which compile edges Ninja reruns under
three collator designs.
Part 2 shows the failures dyndep prevents (missing order, stale BMIs) and the ones that remain
(import cycles, flags mismatches, and ninja issue 2666 with GCC).
Part 3 builds the same sources with CMake's native support and prints what it wrote.
Everything printed is a relative path, a count, or text with run-specific paths scrubbed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CXX = os.environ["CXX"]
SCAN = os.environ["SCAN_DEPS"]
NINJA = os.environ["NINJA"]
CMAKE = os.environ["CMAKE"]
GXX = os.environ.get("GXX", "")
LAB = Path(os.environ["LAB_DIR"])
PY = sys.executable

# Deliberately not in topological order: the dyndep file must discover the order.
SOURCES = ["main.cpp", "report.cppm", "geom_impl.cpp", "geom.cppm", "geom-vec.cppm"]

# Collator designs. A: every transitive BMI is an input, no restat (a naive collator).
# B: what CMake 4.5 writes: direct imports only, restat = 1 on BMI producers.
# C: B plus copy-if-different, the enhancement cmake-cxxmodules(7) lists as future work.
VARIANTS = {
    "A": ("transitive BMI inputs, no restat", []),
    "B": ("direct BMI inputs, restat = 1 (CMake 4.5 shape)", ["--direct-inputs", "--restat"]),
    "C": ("B plus copy-if-different BMIs", ["--direct-inputs", "--restat", "--copy-if-different"]),
}


def stem(src: str) -> str:
    return src.rsplit(".", 1)[0]


def manifest(sources: list[str], variant: str | None) -> str:
    flags = VARIANTS[variant][1] if variant else []
    post = f" && {PY} {LAB}/bmi_commit.py $out.modmap" if "--copy-if-different" in flags else ""
    lines = [
        f"cxx = {CXX}",
        f"scandeps = {SCAN}",
        "cxxflags = -std=c++20",
        "",
        "rule scan",
        "  command = $scandeps -format=p1689 -- $cxx $cxxflags -c $in -o $obj"
        " -MD -MT $out -MF $out.d > $out.tmp && mv $out.tmp $out",
        "  depfile = $out.d",
        "  deps = gcc",
        "  description = SCAN $in",
        "",
        "rule collate",
        f"  command = {PY} {LAB}/collate.py --dd $out --bmi-dir bmi {' '.join(flags)} $in",
        "  restat = 1",
        "  description = COLLATE $out",
        "",
        "rule cxx",
        "  command = $cxx $cxxflags $modflags -c $in -o $out -MD -MF $out.d" + post,
        "  depfile = $out.d",
        "  deps = gcc",
        "  description = CXX $out",
        "",
        "rule link",
        "  command = $cxx $in -o $out",
        "  description = LINK $out",
        "",
    ]
    objs = [f"obj/{stem(s)}.o" for s in sources]
    if variant:
        for s, o in zip(sources, objs):
            lines += [f"build scan/{stem(s)}.ddi: scan src/{s}", f"  obj = {o}"]
        ddis = " ".join(f"scan/{stem(s)}.ddi" for s in sources)
        mods = " ".join(f"{o}.modmap" for o in objs)
        lines.append(f"build modules.dd | {mods}: collate {ddis}")
        for s, o in zip(sources, objs):
            lines += [
                f"build {o}: cxx src/{s} | {o}.modmap || modules.dd",
                "  dyndep = modules.dd",
                f"  modflags = @{o}.modmap",
            ]
    else:
        # An implicit build: BMIs land in a search directory and no edge declares who reads them.
        for s, o in zip(sources, objs):
            out = f" -fmodule-output=bmi/{stem(s)}.pcm" if s.endswith(".cppm") else ""
            lines += [f"build {o}: cxx src/{s}", f"  modflags = -fprebuilt-module-path=bmi{out}"]
    lines.append(f"build app: link {' '.join(objs)}")
    lines.append("default app")
    return "\n".join(lines) + "\n"


def scrub(text: str, *dirs: Path) -> str:
    # Longest paths first, so a build dir never eats the prefix of a sibling source dir.
    forms = sorted({f for d in dirs for f in (str(d.resolve()), str(d))}, key=len, reverse=True)
    for form in forms:
        text = text.replace(form + "/", "").replace(form, "<build>")
    # CMake records the resolved scanner path and the compiler's resource directory.
    text = re.sub(r'"[^"]*/clang-scan-deps"', '"<clang-scan-deps>"', text)
    text = re.sub(r'-resource-dir "[^"]*"', '-resource-dir "<resource-dir>"', text)
    for tool, name in ((CXX, "<cxx>"), (SCAN, "<clang-scan-deps>"), (GXX, "<g++>"), (CMAKE, "<cmake>")):
        if tool:
            text = text.replace(tool, name)
    return text


def ninja(d: Path, *args: str, check: bool = True) -> list[str]:
    env = dict(os.environ, NINJA_STATUS="")
    p = subprocess.run([NINJA, "-C", str(d), "-j1", *args], capture_output=True, text=True, env=env)
    out = [ln for ln in (p.stdout + p.stderr).splitlines() if not ln.startswith("ninja: Entering")]
    if check and p.returncode != 0:
        print("\n".join(out))
        raise SystemExit(f"ninja failed in {d.name}")
    return out


def steps(out: list[str]) -> list[str]:
    return [ln for ln in out if re.match(r"^(SCAN|COLLATE|CXX|LINK) ", ln)]


def compiles(out: list[str]) -> str:
    objs = [ln.split()[1] for ln in steps(out) if ln.startswith("CXX ")]
    return f"{len(objs)} compiles: {', '.join(objs) or '-'}"


def canonical_cycle(ln: str) -> str:
    """Rotate 'dependency cycle: n1 -> n2 -> ... -> n1' to start at the smallest node.

    Ninja starts the message at whichever node it reaches first, which varies between runs.
    """
    m = re.match(r"^(.*dependency cycle: )(.+?)(\.?)$", ln)
    if not m:
        return ln
    nodes = m.group(2).split(" -> ")
    if len(nodes) < 3 or nodes[0] != nodes[-1]:
        return ln
    ring = nodes[:-1]
    i = ring.index(min(ring))
    ring = ring[i:] + ring[:i]
    return m.group(1) + " -> ".join([*ring, ring[0]]) + m.group(3)


def verdict(out: list[str], *dirs: Path) -> str:
    for ln in out:
        if "error" in ln or "dependency cycle" in ln:
            return canonical_cycle(scrub(ln, *dirs))
    return "ok"


def setup(root: Path, name: str, files: dict[str, str], variant: str | None,
          order: list[str] | None = None) -> Path:
    d = root / name
    (d / "src").mkdir(parents=True)
    (d / "bmi").mkdir()
    for fn, body in files.items():
        (d / "src" / fn).write_text(body)
    (d / "build.ninja").write_text(manifest(order or list(files), variant))
    return d


def lab_sources() -> dict[str, str]:
    return {s: (LAB / "src" / s).read_text() for s in SOURCES}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if old not in text:
        raise SystemExit(f"edit target not found in {path.name}: {old!r}")
    path.write_text(text.replace(old, new))


def run_app(d: Path) -> str:
    return subprocess.run([str(d / "app")], capture_output=True, text=True, check=True).stdout.strip()


def section(title: str) -> None:
    print(f"\n== {title}")


def part1(root: Path) -> dict[str, Path]:
    dirs = {v: setup(root, f"variant-{v}", lab_sources(), v, SOURCES) for v in VARIANTS}
    clean = {v: ninja(d) for v, d in dirs.items()}
    b = dirs["B"]

    section("1. P1689R5 scan results (one .ddi per source, written before any compile)")
    for s in SOURCES:
        rule = json.loads((b / "scan" / f"{stem(s)}.ddi").read_text())["rules"][0]
        prov = [p["logical-name"] for p in rule.get("provides", [])]
        req = [r["logical-name"] for r in rule.get("requires", [])]
        print(f"  {s:15} provides={prov or '-'} requires={req or '-'}")
    print("  raw scan/geom.ddi:")
    for ln in (b / "scan" / "geom.ddi").read_text().splitlines():
        print("    " + ln)

    section("2. Collated dyndep file modules.dd, variant B (CMake 4.5 shape)")
    for ln in (b / "modules.dd").read_text().splitlines():
        print("  " + ln)
    print("  variant A main.o line: " + next(
        ln for ln in (dirs["A"] / "modules.dd").read_text().splitlines() if ln.startswith("build obj/main.o")))
    print("  obj/main.o.modmap (every variant names the whole closure for the compiler):")
    for ln in (b / "obj" / "main.o.modmap").read_text().splitlines():
        print("    " + ln)

    section("3. Clean build with -j1, variant B: edge order after Ninja loads the dyndep file")
    for ln in steps(clean["B"]):
        print("  " + ln)
    print("  app prints: " + run_app(b))
    print("  null build: " + " ".join(ninja(b)))

    section("4. Edit the body of a module implementation unit (geom_impl.cpp), variant B")
    edit(b / "src/geom_impl.cpp", "return w * h;", "return h * w;")
    for ln in steps(ninja(b)):
        print("  " + ln)

    section("5. Edit a function body in interface partition geom-vec.cppm")
    for v, d in dirs.items():
        before = {p.name: p.read_bytes() for p in (d / "bmi").glob("*.pcm")}
        edit(d / "src/geom-vec.cppm", "a.x * b.x + a.y * b.y", "a.y * b.y + a.x * b.x")
        out = ninja(d)
        after = {p.name: p.read_bytes() for p in (d / "bmi").glob("*.pcm")}
        changed = sorted(n for n in after if after[n] != before.get(n))
        print(f"  {v} ({VARIANTS[v][0]}): {compiles(out)}")
        print(f"    BMIs whose bytes changed: {', '.join(changed) or 'none'}; app prints: {run_app(d)}")

    section("6. touch geom-vec.cppm without changing its bytes")
    for v, d in dirs.items():
        os.utime(d / "src/geom-vec.cppm")
        print(f"  {v}: {compiles(ninja(d))}")

    section("7. Variant C after report.cppm switches to `export import geom;`, same body edit")
    c = dirs["C"]
    edit(c / "src/report.cppm", "import geom;", "export import geom;")
    ninja(c)
    edit(c / "src/geom-vec.cppm", "a.y * b.y + a.x * b.x", "a.x * b.x + a.y * b.y")
    print(f"  C: {compiles(ninja(c))}")
    return dirs


def part2(root: Path, dirs: dict[str, Path]) -> None:
    section("8. No dyndep: implicit build, BMIs found through -fprebuilt-module-path=bmi")
    naive = setup(root, "implicit", lab_sources(), None, SOURCES)
    print("  ninja obj/main.o on a clean tree: " + verdict(ninja(naive, "obj/main.o", check=False), naive))
    print("  full -j1 build, sources listed main-first: " + verdict(ninja(naive, check=False), naive))
    ordered = setup(root, "implicit-ordered", lab_sources(), None,
                    ["geom-vec.cppm", "geom.cppm", "geom_impl.cpp", "report.cppm", "main.cpp"])
    print("  full -j1 build, sources hand-listed in import order: " + verdict(ninja(ordered, check=False), ordered))
    edit(ordered / "src/report.cppm", "export int summary()", "export int summary2() { return 0; }\nexport int summary()")
    print(f"  then report.cppm gains an export: {compiles(ninja(ordered))} (main.o imports report)")

    section("9. Import cycle a -> b -> a (variant B)")
    cyc = setup(root, "cycle", {"a.cppm": "export module a;\nimport b;\n",
                                "b.cppm": "export module b;\nimport a;\n",
                                "use.cpp": "import a;\nint main() { return 0; }\n"}, "B")
    print("  " + verdict(ninja(cyc, check=False), cyc))

    section("10. Flip an import direction between two builds (ninja issue 2666)")
    flip_files = {"a.cppm": "export module a;\nimport b;\n", "b.cppm": "export module b;\n",
                  "use.cpp": "import a;\nint main() { return 0; }\n"}
    flip = setup(root, "flip-clang", flip_files, "B")
    ninja(flip)
    (flip / "src/a.cppm").write_text("export module a;\n")
    (flip / "src/b.cppm").write_text("export module b;\nimport a;\n")
    print("  hand-written manifest, Clang, build 2 after the flip: " + verdict(ninja(flip, check=False), flip))
    deps = [ln.strip() for ln in ninja(flip, "-t", "deps", "obj/a.o")[1:] if ln.strip()]
    rel = [x for x in deps if not x.startswith("/")]
    print(f"  .ninja_deps for obj/a.o: {', '.join(rel)} (+{len(deps) - len(rel)} absolute SDK path); no BMI")
    if not GXX:
        print("  GCC part skipped: no GCC 14+ found (set GXX)")
        return
    src = root / "flip-gcc-src"
    src.mkdir()
    (src / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.30)\nproject(flip CXX)\nset(CMAKE_CXX_STANDARD 20)\n"
        "add_library(hasmodules)\n"
        "target_sources(hasmodules PRIVATE FILE_SET CXX_MODULES FILES atob.cpp btoa.cpp)\n")
    (src / "atob.cpp").write_text("export module a;\nimport b;\n")
    (src / "btoa.cpp").write_text("export module b;\n")
    bld = root / "flip-gcc"
    cmake_configure(src, bld, GXX)
    ninja(bld)
    (src / "atob.cpp").write_text("export module a;\n")
    (src / "btoa.cpp").write_text("export module b;\nimport a;\n")
    print("  CMake + GCC, build 2 after the flip: " + verdict(ninja(bld, check=False), bld, src))
    deps = [ln.strip() for ln in ninja(bld, "-t", "deps", "CMakeFiles/hasmodules.dir/atob.cpp.o")[1:] if ln.strip()]
    print("  .ninja_deps for atob.cpp.o: " + ", ".join(scrub(x, bld, src) for x in deps))
    (bld / ".ninja_deps").unlink()
    print("  build 3 after deleting .ninja_deps: " + verdict(ninja(bld, check=False), bld, src))

    section("11. Consumer flags must match the BMI: main.cpp at -std=c++23 against C++20 BMIs")
    d = dirs["B"]
    p = subprocess.run([CXX, "-std=c++23", "@obj/main.o.modmap", "-c", "src/main.cpp", "-o", "/dev/null"],
                       cwd=d, capture_output=True, text=True)
    print("  " + verdict(p.stderr.splitlines(), d))


def cmake_configure(src: Path, bld: Path, cxx: str, generator: str = "Ninja") -> subprocess.CompletedProcess:
    args = [CMAKE, "-S", str(src), "-B", str(bld), "-G", generator, f"-DCMAKE_CXX_COMPILER={cxx}"]
    if generator == "Ninja":
        args.append(f"-DCMAKE_MAKE_PROGRAM={NINJA}")
    p = subprocess.run(args, capture_output=True, text=True)
    if generator == "Ninja" and p.returncode != 0:
        print(p.stdout + p.stderr)
        raise SystemExit("cmake configure failed")
    return p


def part3(root: Path) -> None:
    section("12. CMake native support (FILE_SET CXX_MODULES, Ninja generator)")
    src = root / "cmake-src"
    shutil.copytree(LAB / "src", src / "src")
    shutil.copy(LAB / "cmake" / "CMakeLists.txt", src / "CMakeLists.txt")
    b = root / "cmake-build"
    cmake_configure(src, b, CXX)
    ninja(b)
    print("  app prints: " + run_app(b))
    files = sorted(
        str(p.relative_to(b)) for p in b.rglob("*")
        if p.is_file() and (p.suffix in {".ddi", ".dd", ".modmap", ".pcm"}
                            or p.name in {"CXXDependInfo.json", "CXXModules.json"})
    )
    print("  module files CMake wrote:")
    for f in files:
        print("    " + f)
    rules = (b / "CMakeFiles" / "rules.ninja").read_text()
    for kind in ("SCAN", "DYNDEP"):
        m = re.search(rf"^rule CXX_{kind}__app_\S*\n(?:  .*\n)*?  command = (.*)$", rules, re.M)
        if m:
            print(f"  CXX_{kind}__app rule command: " + scrub(m.group(1), b, src))
    tdir = b / "CMakeFiles" / "app.dir"
    for name in ("main.cpp.o.modmap", "report.cppm.o.modmap"):
        mm = next(tdir.rglob(name))
        print(f"  {mm.relative_to(b)}:")
        for ln in mm.read_text().splitlines():
            print("    " + scrub(ln, b, src))
    for dd in sorted(b.glob("CMakeFiles/*.dir/CXX.dd")):
        print(f"  {dd.relative_to(b)}:")
        for ln in dd.read_text().splitlines():
            if ln.strip():
                print("    " + scrub(ln, b, src))
    os.utime(src / "src/geom-vec.cppm")
    objs = [ln for ln in ninja(b) if "Building CXX object" in ln]
    print(f"  touch src/geom-vec.cppm, rebuild: {len(objs)} compiles: "
          + ", ".join(ln.split("Building CXX object ")[1] for ln in objs))

    section("13. CMake with the Unix Makefiles generator")
    mk = cmake_configure(src, root / "cmake-make", CXX, "Unix Makefiles")
    text = scrub(mk.stdout + mk.stderr, root / "cmake-make", src)
    print(f"  configure exit status {mk.returncode}")
    keep = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("--")]
    for ln in keep[:8]:
        print("  " + ln)


def main() -> int:
    root = Path(sys.argv[1])
    os.chdir(root)
    dirs = part1(root)
    part2(root, dirs)
    part3(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
