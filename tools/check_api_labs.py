#!/usr/bin/env python3
"""Gate the 20-API-Engineering labs: traceability, ASCII style, lockfile, Ruff, Mypy, Pyright, contracts, and pytest.

Scope is `20-API-Engineering/`, read through `git ls-files` like every vault tool,
so a new lab or note change is checked once it is staged.

Checks, in execution order:
    traceability  every `- Lab: \\`labs/tests/fNN_<slug>/test_<stem_snake>.py\\`` line in a note
                  points to a tracked test file whose `fNN_<slug>` matches the note's folder and
                  whose file name matches `test_` + the note stem in snake_case; every tracked
                  `labs/tests/**/test_*.py` is declared by exactly one note (no orphans, no double
                  owners); no note with status != `seed` still carries a `Lab plan:` placeholder;
                  and every `fNN_` package under `src/api_labs/` and `tests/` matches a real folder
    ascii         every tracked `.py`, `.toml`, `.yaml`, `.yml`, and `.md` file under `labs/` is
                  strictly 7-bit ASCII with no em or en dashes
    lock          `uv lock --check` in `20-API-Engineering/labs/`
    format        `uv run --frozen ruff format --check src tests`
    lint          `uv run --frozen ruff check src tests`
    types         `uv run --frozen mypy src tests` and `uv run --frozen pyright src tests`
    contracts     validates tracked files under `labs/fixtures/openapi/` with
                  `openapi-spec-validator` via `uv run --frozen` and tracked files under
                  `labs/fixtures/proto/` with `buf lint` (missing `fixtures/` directories pass
                  with a printed note)
    pytest        `uv run --frozen pytest -m "unit or socket" --cov --cov-report=term-missing
                  --cov-fail-under=95` by default, or all markers with `--containers`, bounded by
                  `LAB_SUITE_TIMEOUT` (default 900 seconds) with its process group killed on expiry

Structural checks (`traceability`, `ascii`) run in pure Python without `uv`.
A missing required tool (`uv`, or `buf` when proto fixtures exist) is an error (exit 2), never a skip.
Exit 0 when all selected checks pass, 1 on any check finding, 2 on a missing tool or bad configuration.

Usage (from the repo root):
    python3 tools/check_api_labs.py
    python3 tools/check_api_labs.py --only traceability --only ascii
    python3 tools/check_api_labs.py --containers
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath

SECTION = "20-API-Engineering"
LABS = f"{SECTION}/labs"
OPENAPI_FIXTURES = f"{LABS}/fixtures/openapi"
PROTO_FIXTURES = f"{LABS}/fixtures/proto"
ALL_CHECKS = (
    "traceability",
    "ascii",
    "lock",
    "format",
    "lint",
    "types",
    "contracts",
    "pytest",
)
UV_CHECKS = frozenset({"lock", "format", "lint", "types", "contracts", "pytest"})
ASCII_EXTS = frozenset({".py", ".toml", ".yaml", ".yml", ".md"})
SUITE_TIMEOUT_DEFAULT = "900"
UV_INSTALL_HINT = (
    "install uv: `brew install uv` (macOS) or see 20-API-Engineering/labs/README.md"
)
BUF_INSTALL_HINT = "install buf: `brew install bufbuild/buf/buf` (macOS) or see 20-API-Engineering/labs/README.md"

FOLDER_RE = re.compile(r"^(\d{2})-(.+)$")
PKG_RE = re.compile(r"^f\d{2}_[a-z0-9_]+$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
STATUS_RE = re.compile(r"^status:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
LAB_LINE_RE = re.compile(r"^-\s*Lab:\s*(.*?)\s*$")
LAB_PATH_RE = re.compile(r"^`(labs/tests/([^/`]+)/([^/`]+))`$")
LAB_PLAN_RE = re.compile(r"^(?:-\s*)?Lab plan:")


def tracked(repo: Path) -> list[str]:
    """Tracked files under 20-API-Engineering/."""
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--", SECTION],
        capture_output=True,
        check=True,
    ).stdout
    return sorted(p for p in out.decode().split("\0") if p and (repo / p).is_file())


def strip_fenced_code(text: str) -> list[str]:
    """Return lines outside fenced code blocks."""
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        m = FENCE_RE.match(line)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        out.append(line)
    return out


def note_status(text: str) -> str | None:
    """Return the frontmatter status string, or None when absent."""
    m = FRONTMATTER_RE.match(text)
    s = STATUS_RE.search(m.group(1)) if m else None
    return s.group(1).strip("'\"") if s else None


def folder_to_slug(folder_name: str) -> str | None:
    """Map `NN-Folder-Name` to `fNN_folder_name`, or None if not a numbered folder."""
    m = FOLDER_RE.match(folder_name)
    if not m:
        return None
    nn, rest = m.group(1), m.group(2)
    return f"f{nn}_{rest.lower().replace('-', '_')}"


def stem_to_test_name(stem: str) -> str:
    """Map a note stem to `test_<stem_snake>.py`."""
    snake = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    return f"test_{snake}.py"


def check_traceability(repo: Path, files: list[str]) -> list[str]:
    """Verify note-to-lab declarations, package folders, orphans, double owners, and leftover Lab plans."""
    problems: list[str] = []
    file_set = set(files)

    valid_slugs: set[str] = set()
    for path in files:
        rel = PurePosixPath(path).relative_to(SECTION)
        if len(rel.parts) >= 2:
            slug = folder_to_slug(rel.parts[0])
            if slug is not None:
                valid_slugs.add(slug)

    src_pkgs: set[str] = set()
    test_pkgs: set[str] = set()
    for path in files:
        if path.startswith(f"{LABS}/src/api_labs/"):
            rel = PurePosixPath(path).relative_to(f"{LABS}/src/api_labs")
            if len(rel.parts) >= 2:
                src_pkgs.add(rel.parts[0])
        elif path.startswith(f"{LABS}/tests/"):
            rel = PurePosixPath(path).relative_to(f"{LABS}/tests")
            if len(rel.parts) >= 2:
                test_pkgs.add(rel.parts[0])

    for pkg in sorted(src_pkgs):
        if pkg not in valid_slugs:
            problems.append(
                f"{LABS}/src/api_labs/{pkg}: package directory does not match any {SECTION}/NN-* folder"
            )
    for pkg in sorted(test_pkgs):
        if pkg not in valid_slugs:
            problems.append(
                f"{LABS}/tests/{pkg}: package directory does not match any {SECTION}/NN-* folder"
            )

    owners: dict[str, list[str]] = defaultdict(list)
    for path in files:
        if not path.endswith(".md") or path.startswith(f"{LABS}/"):
            continue
        rel = PurePosixPath(path).relative_to(SECTION)
        if len(rel.parts) != 2 or rel.name == "README.md":
            continue
        expected_slug = folder_to_slug(rel.parts[0])
        if expected_slug is None:
            continue
        expected_test = stem_to_test_name(rel.stem)
        text = (repo / path).read_text(errors="replace")
        status = note_status(text)
        body_lines = strip_fenced_code(text)

        if status != "seed" and any(LAB_PLAN_RE.match(line) for line in body_lines):
            problems.append(
                f"{path}: note with status {status!r} still has a 'Lab plan:' line"
            )

        for line in body_lines:
            m = LAB_LINE_RE.match(line)
            if not m:
                continue
            raw_target = m.group(1)
            pm = LAB_PATH_RE.match(raw_target)
            if not pm:
                problems.append(
                    f"{path}: malformed '- Lab:' declaration {raw_target!r} "
                    f"(expected `labs/tests/{expected_slug}/{expected_test}`)"
                )
                continue
            rel_lab, lab_pkg, lab_file = pm.group(1), pm.group(2), pm.group(3)
            full_lab = f"{SECTION}/{rel_lab}"
            owners[full_lab].append(path)
            if full_lab not in file_set:
                problems.append(f"{path}: declared lab is not tracked: {rel_lab}")
            if lab_pkg != expected_slug:
                problems.append(
                    f"{path}: declared lab folder {lab_pkg!r} does not match note folder {expected_slug!r}"
                )
            if lab_file != expected_test:
                problems.append(
                    f"{path}: declared lab file {lab_file!r} does not match note stem (expected {expected_test!r})"
                )

    tracked_tests = [
        p
        for p in files
        if p.startswith(f"{LABS}/tests/")
        and PurePosixPath(p).name.startswith("test_")
        and p.endswith(".py")
    ]
    for test_path in tracked_tests:
        claimants = owners.get(test_path, [])
        if not claimants:
            problems.append(f"{test_path}: orphan test file not declared by any note")
        elif len(claimants) > 1:
            problems.append(
                f"{test_path}: declared by multiple notes ({', '.join(claimants)})"
            )

    return problems


def check_ascii(repo: Path, files: list[str]) -> list[str]:
    """Verify every tracked .py, .toml, .yaml, .yml, and .md file under labs/ is 7-bit ASCII without em/en dashes."""
    problems: list[str] = []
    for path in files:
        if not path.startswith(f"{LABS}/"):
            continue
        if PurePosixPath(path).suffix.lower() not in ASCII_EXTS:
            continue
        raw = (repo / path).read_bytes()
        if raw.isascii():
            continue
        text = raw.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "\u2014" in line or "\u2013" in line:
                problems.append(f"{path}:{lineno}: contains em or en dash (non-ASCII)")
            elif not line.isascii():
                problems.append(f"{path}:{lineno}: contains non-ASCII characters")
    return problems


def run(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None
) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, env=env, check=False
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def clean_signals() -> None:
    """Reset inherited SIG_IGN dispositions and signal masks before spawning child suites."""
    if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    if signal.getsignal(signal.SIGQUIT) == signal.SIG_IGN:
        signal.signal(signal.SIGQUIT, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_SETMASK, [])


def run_bounded(
    cmd: list[str], cwd: Path, env: dict[str, str], limit: float
) -> tuple[int, str]:
    """Run cmd in its own session and kill its process group after limit seconds."""
    with tempfile.TemporaryFile("w+") as out:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        note = ""
        try:
            rc = proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            rc, note = 1, f"\nsuite killed after LAB_SUITE_TIMEOUT={limit:g} seconds"
        out.seek(0)
        return rc, out.read().strip() + note


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument(
        "--only",
        action="append",
        choices=ALL_CHECKS,
        metavar="CHECK",
        help=f"run only the named check(s): {', '.join(ALL_CHECKS)}",
    )
    ap.add_argument(
        "--containers",
        action="store_true",
        help="include @pytest.mark.container tests in the pytest check",
    )
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    labs_dir = repo / LABS
    selected = list(dict.fromkeys(args.only)) if args.only else list(ALL_CHECKS)

    clean_signals()
    try:
        suite_limit = float(os.environ.get("LAB_SUITE_TIMEOUT", SUITE_TIMEOUT_DEFAULT))
    except ValueError:
        print(
            f"error: LAB_SUITE_TIMEOUT must be a number of seconds, not {os.environ['LAB_SUITE_TIMEOUT']!r}",
            file=sys.stderr,
        )
        return 2

    files = tracked(repo)
    lab_files = [p for p in files if p.startswith(f"{LABS}/")]
    if not files:
        print(f"api labs: 0 files tracked under {LABS}; nothing to check")
        return 0

    openapi_files = [
        str(PurePosixPath(p).relative_to(LABS))
        for p in lab_files
        if p.startswith(f"{OPENAPI_FIXTURES}/")
    ]
    proto_files = [
        str(PurePosixPath(p).relative_to(LABS))
        for p in lab_files
        if p.startswith(f"{PROTO_FIXTURES}/")
    ]

    needs_uv = bool(set(selected) & UV_CHECKS)
    if needs_uv and shutil.which("uv") is None:
        print(f"error: uv not found on PATH; {UV_INSTALL_HINT}", file=sys.stderr)
        return 2
    if "contracts" in selected and proto_files and shutil.which("buf") is None:
        print(f"error: buf not found on PATH; {BUF_INSTALL_HINT}", file=sys.stderr)
        return 2

    test_files = [
        p
        for p in lab_files
        if p.startswith(f"{LABS}/tests/")
        and PurePosixPath(p).name.startswith("test_")
        and p.endswith(".py")
    ]
    print(
        f"api labs: {len(lab_files)} tracked lab files, {len(test_files)} test modules "
        f"(checks: {', '.join(selected)})"
    )

    failed: list[str] = []

    def report(name: str, rc: int, output: str) -> None:
        if rc != 0:
            failed.append(name)
            print(f"FAIL {name}")
            if output:
                print(output)

    for check in selected:
        if check == "traceability":
            problems = check_traceability(repo, files)
            report("traceability", 1 if problems else 0, "\n".join(problems))
        elif check == "ascii":
            problems = check_ascii(repo, files)
            report("ascii", 1 if problems else 0, "\n".join(problems))
        elif check == "lock":
            report("lock", *run(["uv", "lock", "--check"], labs_dir))
        elif check == "format":
            report(
                "format",
                *run(
                    [
                        "uv",
                        "run",
                        "--frozen",
                        "ruff",
                        "format",
                        "--check",
                        "src",
                        "tests",
                    ],
                    labs_dir,
                ),
            )
        elif check == "lint":
            report(
                "lint",
                *run(
                    ["uv", "run", "--frozen", "ruff", "check", "src", "tests"],
                    labs_dir,
                ),
            )
        elif check == "types":
            mypy_rc, mypy_out = run(
                ["uv", "run", "--frozen", "mypy", "src", "tests"], labs_dir
            )
            pyright_rc, pyright_out = run(
                ["uv", "run", "--frozen", "pyright", "src", "tests"], labs_dir
            )
            combined_out = "\n".join(out for out in (mypy_out, pyright_out) if out)
            report("types", max(mypy_rc, pyright_rc), combined_out)
        elif check == "contracts":
            contract_rc = 0
            contract_outs: list[str] = []
            if not openapi_files and not proto_files:
                print(
                    f"contracts: no fixtures tracked under {OPENAPI_FIXTURES} or {PROTO_FIXTURES}; pass"
                )
            if openapi_files:
                for spec in openapi_files:
                    rc, out = run(
                        ["uv", "run", "--frozen", "openapi-spec-validator", spec],
                        labs_dir,
                    )
                    if rc != 0:
                        contract_rc = 1
                        contract_outs.append(f"{spec}: {out}" if out else spec)
            if proto_files:
                rc, out = run(["buf", "lint", "fixtures/proto"], labs_dir)
                if rc != 0:
                    contract_rc = 1
                    if out:
                        contract_outs.append(out)
            report("contracts", contract_rc, "\n".join(contract_outs))
        elif check == "pytest":
            marker_args = [] if args.containers else ["-m", "unit or socket"]
            pytest_cmd = [
                "uv",
                "run",
                "--frozen",
                "pytest",
                *marker_args,
                "--cov",
                "--cov-report=term-missing",
                "--cov-fail-under=95",
            ]
            report(
                "pytest",
                *run_bounded(pytest_cmd, labs_dir, dict(os.environ), suite_limit),
            )

    print(f"result: FAIL ({', '.join(failed)})" if failed else "result: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
