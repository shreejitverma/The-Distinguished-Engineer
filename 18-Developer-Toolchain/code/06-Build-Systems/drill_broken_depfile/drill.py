#!/usr/bin/env python3
"""Drive the broken-depfile drill: run each failure in isolation against the broken and fixed setups.

Usage: python3 drill.py LAB_DIR SCRATCH_DIR

Every scenario builds a fresh copy of project/ in SCRATCH_DIR, so scenarios cannot leak state.
Output is deterministic: mtimes, paths, and compiler wording are reduced to derived facts.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

LAB = Path(sys.argv[1]).resolve()
SCRATCH = Path(sys.argv[2]).resolve()
CC = os.environ.get("CC", "cc")
PY = sys.executable
STEP_RE = re.compile(r"^\[\d+/\d+\] (.+)$")
TMP_PREFIX = ".schemagen-"
# Manifest variants derived from fixed.ninja, so each scenario changes exactly one thing.
NO_RESTAT = ("  restat = 1\n", "")
NO_DEPS = ("  depfile = $out.d\n  deps = gcc\n  # FIX", "  depfile = $out.d\n  # FIX")


class Tree:
    """One scratch build tree: project sources, a generator, and a manifest."""

    def __init__(self, name: str, generator: str, manifest: str, edits: tuple[tuple[str, str], ...] = ()) -> None:
        self.dir = SCRATCH / name
        shutil.copytree(LAB / "project", self.dir)
        (self.dir / "tools").mkdir()
        shutil.copy(LAB / f"schemagen_{generator}.py", self.dir / "tools" / "schemagen.py")
        text = (LAB / "manifests" / f"{manifest}.ninja").read_text()
        text = text.replace("python = python3", f"python = {PY}").replace("cc = cc", f"cc = {CC}")
        for old, new in edits:
            if text.count(old) != 1:
                raise SystemExit(f"manifest {manifest}: {old!r} must occur exactly once")
            text = text.replace(old, new)
        (self.dir / "build.ninja").write_text(text)

    def ninja(self, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
        full_env = dict(os.environ, NINJA_STATUS="[%f/%t] ", **(env or {}))
        p = subprocess.run(["ninja", *args], cwd=self.dir, capture_output=True, text=True, env=full_env)
        if check and p.returncode != 0:
            sys.stderr.write(p.stdout + p.stderr)
            raise SystemExit(f"{self.dir.name}: ninja {' '.join(args)} failed with {p.returncode}")
        return p

    def steps(self, *args: str, env: dict | None = None) -> str:
        p = self.ninja(*args, env=env)
        found = [m.group(1) for line in p.stdout.splitlines() if (m := STEP_RE.match(line))]
        if not found:
            if "no work to do" not in p.stdout:
                raise SystemExit(f"{self.dir.name}: unexpected ninja output:\n{p.stdout}")
            return "no work to do"
        return ", ".join(found)

    def planned(self) -> str:
        return self.steps("-n")

    def explain(self, needle: str) -> list[str]:
        p = self.ninja("-n", "-d", "explain")
        lines = [ln.removeprefix("ninja explain: ") for ln in p.stderr.splitlines() if needle in ln]
        return [re.sub(r"\(\d+ vs \d+\)", "(MTIME vs MTIME)", ln) for ln in lines]

    def deps(self, target: str) -> str:
        out = self.ninja("-t", "deps", target).stdout.splitlines()
        head = re.sub(r"deps mtime \d+", "deps mtime MTIME", out[0])
        return head + " -> " + " | ".join(ln.strip() for ln in out[1:] if ln.strip())

    def app(self) -> str:
        return subprocess.run(["./app"], cwd=self.dir, capture_output=True, text=True, check=True).stdout.strip()

    def edit(self, rel: str, old: str | None, new: str) -> None:
        """Rewrite a file after a short pause, so its mtime is strictly newer than the last build's."""
        time.sleep(0.05)
        path = self.dir / rel
        text = path.read_text()
        if old is None:
            text += new
        else:
            if old not in text:
                raise SystemExit(f"{rel}: {old!r} not found")
            text = text.replace(old, new)
        path.write_text(text)


def sort_adjacent_compiles(steps: str) -> str:
    """Sort each run of consecutive CC steps: compiles that ninja -j runs together finish in any order.

    Non-adjacent order is kept, because it is the evidence (a CC before GEN means it did not wait).
    """
    out: list[str] = []
    run: list[str] = []
    for step in steps.split(", "):
        if step.startswith("CC "):
            run.append(step)
            continue
        out.extend(sorted(run))
        run = []
        out.append(step)
    out.extend(sorted(run))
    return ", ".join(out)


def header(title: str) -> None:
    print(f"\n=== {title} ===")


# Scenarios 1 to 5 build with fixed.ninja (or a one-line variant of it) for both generators,
# so only the generator differs; scenario 6 holds the generator fixed and varies the manifest.


def scenario_transitive() -> None:
    header("1. Edit a schema imported two levels down (version.schema: SCHEMA_VERSION 1 -> 2)")
    for gen in ("broken", "fixed"):
        t = Tree(f"s1-{gen}", gen, "fixed")
        t.steps()
        print(f"[{gen} generator] recorded deps: {t.deps('gen/order.h')}")
        t.edit("schemas/version.schema", "SCHEMA_VERSION 1", "SCHEMA_VERSION 2")
        print(f"[{gen} generator] rebuild after the edit: {t.steps()}")
        print(f"[{gen} generator] app: {t.app()}")


def scenario_spaces() -> None:
    header("2. Import a path that contains a space (vendor schemas/money.schema)")
    for gen in ("broken", "fixed"):
        t = Tree(f"s2-{gen}", gen, "fixed")
        t.edit("schemas/order.schema", None, "import vendor schemas/money.schema\n")
        t.steps()
        print(f"[{gen} generator] recorded deps: {t.deps('gen/order.h')}")
        for line in t.explain(" is dirty"):
            print(f"[{gen} generator] explain: {line}")
        print(f"[{gen} generator] second build, nothing edited: {t.steps()}")
        print(f"[{gen} generator] third build, nothing edited: {t.steps()}")


def scenario_target_name() -> None:
    header("3. Depfile target name: deps = gcc versus a plain depfile")
    for gen in ("broken", "fixed"):
        t = Tree(f"s3-gcc-{gen}", gen, "fixed", edits=(NO_RESTAT,))
        t.steps()
        print(f"[{gen} generator, deps = gcc] second build, nothing edited: {t.steps()}")
    for gen in ("broken", "fixed"):
        # Without `deps`, Ninja re-reads gen/order.h.d at every startup and checks its target.
        t = Tree(f"s3-plain-{gen}", gen, "fixed", edits=(NO_RESTAT, NO_DEPS))
        t.steps()
        for line in t.explain("expected depfile"):
            print(f"[{gen} generator, plain depfile] explain: {line}")
        print(f"[{gen} generator, plain depfile] second build, nothing edited: {t.steps()}")


def scenario_restat() -> None:
    header("4. Comment-only schema edit: write-if-changed and restat")
    configs = (
        ("broken generator, no restat", "broken", "fixed", (NO_RESTAT,)),
        ("fixed generator, no restat", "fixed", "fixed", (NO_RESTAT,)),
        ("fixed generator, restat = 1", "fixed", "fixed", ()),
    )
    for i, (label, gen, manifest, edits) in enumerate(configs):
        t = Tree(f"s4-{i}", gen, manifest, edits=edits)
        t.steps()
        t.edit("schemas/order.schema", "# Order wire format", "# Order wire format (reviewed)")
        print(f"[{label}] build after the edit: {t.steps()}")
        print(f"[{label}] next build, nothing edited: {t.steps()}")
        print(f"[{label}] and the next: {t.steps()}")


def scenario_crash() -> None:
    header("5. Generator dies halfway through writing the header")
    for gen in ("broken", "fixed"):
        t = Tree(f"s5-{gen}", gen, "fixed")
        t.steps()
        t.edit("schemas/order.schema", "field qty int32_t", "field qty int32_t\nfield side int8_t")
        p = t.ninja(env={"SCHEMAGEN_CRASH_MIDWRITE": "1"}, check=False)
        text = (t.dir / "gen" / "order.h").read_text()
        state = "complete (old contents)" if text.rstrip().endswith("*/") and "side" not in text else "TRUNCATED"
        leftovers = len([f for f in os.listdir(t.dir / "gen") if f.startswith(TMP_PREFIX)])
        print(f"[{gen} generator] ninja exit status: {p.returncode}")
        print(f"[{gen} generator] gen/order.h on disk after the crash: {state}")
        print(f"[{gen} generator] leftover temporary files in gen/: {leftovers}")
        print(f"[{gen} generator] what the next build plans: {t.planned()}")


def scenario_race() -> None:
    header("6. Generated-header ordering under -j4 with a slow generator (fixed generator in both)")
    slow = {"SCHEMAGEN_DELAY": "1.5"}
    for manifest in ("broken", "fixed"):
        t = Tree(f"s6-clean-{manifest}", "fixed", manifest)
        p = t.ninja("-j4", env=slow, check=False)
        missing = "order.h" in p.stdout and "FAILED" in p.stdout
        verdict = "FAILED, the compiler could not find order.h" if missing else f"exit {p.returncode}"
        if p.returncode == 0:
            verdict = "succeeded"
        print(f"[{manifest} manifest] clean build, -j4: {verdict}")
    for manifest in ("broken", "fixed"):
        t = Tree(f"s6-incr-{manifest}", "fixed", manifest)
        t.steps("gen/order.h")
        t.steps()
        # One change set: a new #include of the generated header plus a schema edit that changes it.
        time.sleep(0.05)
        shutil.copy(t.dir / "src" / "report_with_include.c", t.dir / "src" / "report.c")
        t.edit("schemas/version.schema", "SCHEMA_VERSION 1", "SCHEMA_VERSION 2")
        print(f"[{manifest} manifest] incremental build, -j4: {sort_adjacent_compiles(t.steps('-j4', env=slow))}")
        print(f"[{manifest} manifest] app: {t.app()}")
        md = t.ninja("-t", "missingdeps", check=False)
        for line in md.stdout.splitlines():
            print(f"[{manifest} manifest] missingdeps: {line}")
        print(f"[{manifest} manifest] missingdeps exit status: {md.returncode}")
        print(f"[{manifest} manifest] what the next build plans: {t.planned()}")


def scenario_cmake() -> None:
    header("7. The same generators under CMake's Ninja generator (add_custom_command DEPFILE)")
    for gen in ("broken", "fixed"):
        src = SCRATCH / f"s7-{gen}-src"
        bld = SCRATCH / f"s7-{gen}-build"
        shutil.copytree(LAB / "project", src)
        shutil.copy(LAB / "cmake" / "CMakeLists.txt", src / "CMakeLists.txt")
        shutil.copy(LAB / f"schemagen_{gen}.py", src / "schemagen.py")
        subprocess.run(
            ["cmake", "-S", str(src), "-B", str(bld), "-G", "Ninja", f"-DPYTHON={PY}",
             f"-DSCHEMAGEN={src / 'schemagen.py'}", f"-DCMAKE_C_COMPILER={shutil.which(CC) or CC}"],
            capture_output=True, text=True, check=True)
        if gen == "broken":
            facts = cmake_manifest_facts(bld / "build.ninja")
            for key, value in facts:
                print(f"[CMake build.ninja] {key}: {value}")
        run = lambda *a: subprocess.run(["ninja", "-C", str(bld), *a], capture_output=True, text=True,
                                        check=True, env=dict(os.environ, NINJA_STATUS="[%f/%t] "))
        run()
        time.sleep(0.05)
        version = src / "schemas" / "version.schema"
        version.write_text(version.read_text().replace("SCHEMA_VERSION 1", "SCHEMA_VERSION 2"))
        out = run().stdout
        steps = [m.group(1) for ln in out.splitlines() if (m := STEP_RE.match(ln))]
        print(f"[CMake, {gen} generator] rebuild after editing version.schema: {len(steps)} steps"
              + (" (" + ", ".join(cmake_step_label(s) for s in steps) + ")" if steps else ""))
        app = subprocess.run([str(bld / "app")], capture_output=True, text=True, check=True).stdout.strip()
        print(f"[CMake, {gen} generator] app: {app}")


def cmake_step_label(step: str) -> str:
    if step.startswith("GEN"):
        return "GEN"
    if "C object" in step:
        return "CC " + step.rsplit("/", 1)[-1]
    if "Linking" in step:
        return "LINK"
    return step


def cmake_manifest_facts(path: Path) -> list[tuple[str, str]]:
    blocks = path.read_text().split("\n\n")

    def block(pattern: str) -> str:
        found = [b for b in blocks if re.search(pattern, b, re.MULTILINE)]
        if len(found) != 1:
            raise SystemExit(f"{path}: expected one build block matching {pattern!r}, found {len(found)}")
        return found[0]

    gen = block(r"^build gen/order\.h( |:).*CUSTOM_COMMAND")
    obj = block(r"^build CMakeFiles/app\.dir/src/main\.c\.o:")
    first = next(ln for ln in obj.splitlines() if ln.startswith("build "))
    order_only = first.split("||", 1)[1].split() if "||" in first else []
    phony = block(rf"^build {re.escape(order_only[0])}: phony") if order_only else ""
    yes = lambda cond: "yes" if cond else "no"
    return [
        ("custom command edge sets deps = gcc", yes(re.search(r"^  deps = gcc$", gen, re.M))),
        ("custom command edge sets restat = 1", yes(re.search(r"^  restat = 1$", gen, re.M))),
        ("Ninja reads CMake's transformed copy under CMakeFiles/d/",
         yes(re.search(r"^  depfile = CMakeFiles/d/[0-9a-f]+\.d$", gen, re.M))),
        ("command runs cmake -E cmake_transform_depfile", yes("cmake_transform_depfile" in gen)),
        ("output also declared under its absolute path", yes("| ${cmake_ninja_workdir}gen/order.h" in gen)),
        ("main.c.o has an order-only dependency", yes(order_only)),
        ("that order-only phony depends on gen/order.h", yes("|| gen/order.h" in phony)),
    ]


def main() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    scenario_transitive()
    scenario_spaces()
    scenario_target_name()
    scenario_restat()
    scenario_crash()
    scenario_race()
    scenario_cmake()


if __name__ == "__main__":
    main()
