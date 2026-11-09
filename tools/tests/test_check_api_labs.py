"""Behavior tests for tools/check_api_labs.py: run `python3 -m unittest discover tools/tests`.

Pure checks (`traceability`, `ascii`) run against temporary git repositories with no `uv` on PATH.
Subprocess checks (`lock`, `format`, `lint`, `types`, `contracts`, `pytest`) use fake `uv` and `buf`
executables on PATH that log their arguments and return controlled exit codes, so the real `uv` is
never invoked and every check in `check_api_labs.py` has a test that fails if the check is removed.
`LabTierMarkerTest` runs the labs' real pytest through `uv` against the real `labs/tests/conftest.py`
and is skipped only when `uv` is not on PATH.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "check_api_labs.py"
SECTION = "20-API-Engineering"
LABS = f"{SECTION}/labs"
FOLDER_01 = f"{SECTION}/01-HTTP-Semantics-and-Wire-Protocols"
SLUG_01 = "f01_http_semantics_and_wire_protocols"
NOTE_STEM = "HTTP-1-1-Framing-Keep-Alive-and-Chunked-Encoding"
TEST_NAME = "test_http_1_1_framing_keep_alive_and_chunked_encoding.py"
GIT = shutil.which("git")
UV = shutil.which("uv")
REAL_LABS = Path(__file__).resolve().parent.parent.parent / LABS

SEED_NOTE = f"""---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# HTTP/1.1 Wire Framing

## Related

Lab plan: Proves chunked transfer coding.
- Lab: `labs/tests/{SLUG_01}/{TEST_NAME}`
"""

FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}/uv.log"
case "$*" in
  "lock --check")
    exit "${{FAKE_LOCK_RC:-0}}"
    ;;
  "run --frozen ruff format --check src tests")
    exit "${{FAKE_FORMAT_RC:-0}}"
    ;;
  "run --frozen ruff check src tests")
    exit "${{FAKE_LINT_RC:-0}}"
    ;;
  "run --frozen mypy src tests")
    if [ -n "${{FAKE_MYPY_SIGKILL:-}}" ]; then kill -9 $$; fi
    exit "${{FAKE_MYPY_RC:-0}}"
    ;;
  "run --frozen pyright src tests")
    exit "${{FAKE_PYRIGHT_RC:-0}}"
    ;;
  "run --frozen openapi-spec-validator "*)
    exit "${{FAKE_OPENAPI_RC:-0}}"
    ;;
  "run --frozen pytest "*)
    exit "${{FAKE_PYTEST_RC:-0}}"
    ;;
esac
exit 0
"""

FAKE_BUF = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}/buf.log"
exit "${{FAKE_BUF_RC:-0}}"
"""


class CheckApiLabsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo, self.bin, self.log = root / "repo", root / "bin", root / "log"
        for d in (self.repo, self.bin, self.log):
            d.mkdir()
        assert GIT is not None
        self.write_exe(self.bin / "git", f'#!/bin/sh\nexec "{GIT}" "$@"\n')
        subprocess.run([GIT, "init", "-q", str(self.repo)], check=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def write_exe(path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def install_fake_uv(self) -> None:
        self.write_exe(self.bin / "uv", FAKE_UV.format(log=self.log))

    def install_fake_buf(self) -> None:
        self.write_exe(self.bin / "buf", FAKE_BUF.format(log=self.log))

    def add(self, rel: str, text: str = "", track: bool = True) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if track:
            assert GIT is not None
            subprocess.run([GIT, "-C", str(self.repo), "add", rel], check=True)

    def add_valid_workspace(self) -> None:
        self.add(f"{FOLDER_01}/{NOTE_STEM}.md", SEED_NOTE)
        self.add(f"{LABS}/pyproject.toml", '[project]\nname = "api-engineering-labs"\n')
        self.add(f"{LABS}/README.md", "# Labs\n")
        self.add(f"{LABS}/src/api_labs/__init__.py", "")
        self.add(f"{LABS}/src/api_labs/{SLUG_01}/__init__.py", "")
        self.add(f"{LABS}/src/api_labs/{SLUG_01}/chunked.py", "x = 1\n")
        self.add(f"{LABS}/tests/conftest.py", "")
        self.add(f"{LABS}/tests/{SLUG_01}/__init__.py", "")
        self.add(
            f"{LABS}/tests/{SLUG_01}/{TEST_NAME}",
            "def test_ok() -> None:\n    assert True\n",
        )

    def run_tool(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        full_env = {"PATH": str(self.bin), "HOME": self.tmp.name, **env}
        return subprocess.run(
            [sys.executable, str(TOOL), "--repo", str(self.repo), *args],
            capture_output=True,
            text=True,
            env=full_env,
            timeout=60,
            check=False,
        )

    def calls(self, name: str) -> list[str]:
        path = self.log / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_structural_checks_pass_without_uv_installed(self) -> None:
        self.add_valid_workspace()
        result = self.run_tool("--only", "traceability", "--only", "ascii")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("result: PASS", result.stdout)
        self.assertEqual(self.calls("uv"), [])

    def test_traceability_fails_when_declared_lab_file_is_missing(self) -> None:
        self.add(f"{FOLDER_01}/{NOTE_STEM}.md", SEED_NOTE)
        result = self.run_tool("--only", "traceability")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL traceability", result.stdout)
        self.assertIn("declared lab is not tracked", result.stdout)

    def test_traceability_fails_when_folder_nn_does_not_match(self) -> None:
        self.add_valid_workspace()
        slug_02 = "f02_tls_connections_and_intermediaries"
        self.add(
            f"{SECTION}/02-TLS-Connections-and-Intermediaries/README.md", "# TLS\n"
        )
        self.add(f"{LABS}/tests/{slug_02}/{TEST_NAME}", "def test_x() -> None: pass\n")
        bad_note = SEED_NOTE.replace(SLUG_01, slug_02)
        self.add(f"{FOLDER_01}/{NOTE_STEM}.md", bad_note)
        result = self.run_tool("--only", "traceability")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL traceability", result.stdout)
        self.assertIn("does not match note folder", result.stdout)

    def test_traceability_fails_when_test_file_name_does_not_match_note_stem(
        self,
    ) -> None:
        self.add(
            f"{LABS}/tests/{SLUG_01}/test_wrong_name.py", "def test_x() -> None: pass\n"
        )
        bad_note = SEED_NOTE.replace(TEST_NAME, "test_wrong_name.py")
        self.add(f"{FOLDER_01}/{NOTE_STEM}.md", bad_note)
        result = self.run_tool("--only", "traceability")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL traceability", result.stdout)
        self.assertIn("does not match note stem", result.stdout)

    def test_traceability_fails_on_orphan_test_file(self) -> None:
        self.add_valid_workspace()
        self.add(
            f"{LABS}/tests/{SLUG_01}/test_orphan_module.py",
            "def test_x() -> None: pass\n",
        )
        result = self.run_tool("--only", "traceability")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL traceability", result.stdout)
        self.assertIn("orphan test file not declared by any note", result.stdout)

    def test_traceability_fails_when_two_notes_declare_same_test(self) -> None:
        self.add_valid_workspace()
        self.add(f"{FOLDER_01}/Second-Note.md", SEED_NOTE)
        result = self.run_tool("--only", "traceability")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL traceability", result.stdout)
        self.assertIn("declared by multiple notes", result.stdout)

    def test_traceability_fails_when_drafted_note_keeps_lab_plan_line(self) -> None:
        self.add_valid_workspace()
        draft_note = SEED_NOTE.replace("status: seed", "status: draft")
        self.add(f"{FOLDER_01}/{NOTE_STEM}.md", draft_note)
        result = self.run_tool("--only", "traceability")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL traceability", result.stdout)
        self.assertIn("still has a 'Lab plan:' line", result.stdout)

    def test_traceability_fails_on_stray_fnn_package_in_src_or_tests(self) -> None:
        self.add_valid_workspace()
        self.add(f"{LABS}/src/api_labs/f99_stray_pkg/__init__.py", "")
        self.add(f"{LABS}/tests/f98_other_stray/__init__.py", "")
        result = self.run_tool("--only", "traceability")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL traceability", result.stdout)
        self.assertIn("f99_stray_pkg: package directory does not match", result.stdout)
        self.assertIn(
            "f98_other_stray: package directory does not match", result.stdout
        )

    def test_ascii_fails_when_python_file_contains_em_dash(self) -> None:
        self.add_valid_workspace()
        self.add(
            f"{LABS}/src/api_labs/{SLUG_01}/chunked.py",
            "# bad comment \u2014 em dash\nx = 1\n",
        )
        result = self.run_tool("--only", "ascii")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL ascii", result.stdout)
        self.assertIn("em or en dash", result.stdout)

    def test_ascii_fails_when_only_non_ascii_is_a_unicode_line_separator(self) -> None:
        self.add_valid_workspace()
        self.add(
            f"{LABS}/src/api_labs/{SLUG_01}/chunked.py",
            "x = 1  # separator\u2028here\ny = 2\n",
        )
        result = self.run_tool("--only", "ascii")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            f"{LABS}/src/api_labs/{SLUG_01}/chunked.py:1: contains non-ASCII characters",
            result.stdout,
        )

    def test_missing_uv_exits_2_for_non_structural_checks(self) -> None:
        self.add_valid_workspace()
        result = self.run_tool("--only", "lock")
        self.assertEqual(result.returncode, 2)
        self.assertIn("uv not found on PATH", result.stderr)

    def test_all_checks_run_and_pass_with_fake_uv(self) -> None:
        self.add_valid_workspace()
        self.install_fake_uv()
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("result: PASS", result.stdout)
        self.assertEqual(
            self.calls("uv"),
            [
                "lock --check",
                "run --frozen ruff format --check src tests",
                "run --frozen ruff check src tests",
                "run --frozen mypy src tests",
                "run --frozen pyright src tests",
                "run --frozen pytest --cov --cov-report=term-missing --cov-fail-under=95",
            ],
        )

    def test_lock_check_failure_fails_gate(self) -> None:
        self.add_valid_workspace()
        self.install_fake_uv()
        result = self.run_tool("--only", "lock", FAKE_LOCK_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("result: FAIL (lock)", result.stdout)

    def test_format_check_failure_fails_gate(self) -> None:
        self.add_valid_workspace()
        self.install_fake_uv()
        result = self.run_tool("--only", "format", FAKE_FORMAT_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("result: FAIL (format)", result.stdout)

    def test_lint_check_failure_fails_gate(self) -> None:
        self.add_valid_workspace()
        self.install_fake_uv()
        result = self.run_tool("--only", "lint", FAKE_LINT_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("result: FAIL (lint)", result.stdout)

    def test_types_check_failure_fails_gate(self) -> None:
        self.add_valid_workspace()
        self.install_fake_uv()
        for env in (
            {"FAKE_PYRIGHT_RC": "1"},
            {"FAKE_MYPY_RC": "1"},
            {"FAKE_MYPY_SIGKILL": "1"},
        ):
            with self.subTest(**env):
                result = self.run_tool("--only", "types", **env)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("result: FAIL (types)", result.stdout)

    def test_contracts_validates_openapi_and_proto_fixtures_and_requires_buf(
        self,
    ) -> None:
        self.add_valid_workspace()
        self.install_fake_uv()
        self.add(f"{LABS}/fixtures/openapi/petstore.yaml", "openapi: 3.1.0\n")
        self.add(f"{LABS}/fixtures/proto/service.proto", 'syntax = "proto3";\n')

        missing_buf = self.run_tool("--only", "contracts")
        self.assertEqual(missing_buf.returncode, 2)
        self.assertIn("buf not found on PATH", missing_buf.stderr)

        self.install_fake_buf()
        ok = self.run_tool("--only", "contracts")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn(
            "run --frozen openapi-spec-validator fixtures/openapi/petstore.yaml",
            self.calls("uv"),
        )
        self.assertEqual(self.calls("buf"), ["lint fixtures/proto"])

        fail_openapi = self.run_tool("--only", "contracts", FAKE_OPENAPI_RC="1")
        self.assertEqual(fail_openapi.returncode, 1)
        self.assertIn("result: FAIL (contracts)", fail_openapi.stdout)

    def test_pytest_check_failure_and_timeout(self) -> None:
        self.add_valid_workspace()
        self.install_fake_uv()
        fail_res = self.run_tool("--only", "pytest", FAKE_PYTEST_RC="1")
        self.assertEqual(fail_res.returncode, 1)
        self.assertIn("result: FAIL (pytest)", fail_res.stdout)

        stray = (
            f"import os, time; os.setpgid(0, 0); "
            f'open("{self.log}/stray.pid", "w").write(str(os.getpid())); time.sleep(30)'
        )
        self.write_exe(
            self.bin / "uv",
            f"#!/bin/sh\n'{sys.executable}' -c '{stray}' &\nexec /bin/sleep 30\n",
        )
        self.addCleanup(self.kill_stray)
        started = time.monotonic()
        timed_out = self.run_tool("--only", "pytest", LAB_SUITE_TIMEOUT="1")
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(timed_out.returncode, 1)
        self.assertIn(
            "suite killed after LAB_SUITE_TIMEOUT=1 seconds", timed_out.stdout
        )
        self.assertIn("result: FAIL (pytest)", timed_out.stdout)

    def kill_stray(self) -> None:
        pid_file = self.log / "stray.pid"
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 9)


@unittest.skipUnless(UV, "uv is required to run the labs' real pytest")
class LabTierMarkerTest(unittest.TestCase):
    """The labs conftest must turn a test without exactly one tier marker into pytest exit 4."""

    def run_lab_pytest(self, source: str) -> subprocess.CompletedProcess[str]:
        assert UV is not None
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / "tests"
            tests.mkdir()
            shutil.copy(REAL_LABS / "tests" / "conftest.py", tests / "conftest.py")
            (tests / "test_tiers.py").write_text(source)
            return subprocess.run(
                [
                    UV,
                    "run",
                    "--frozen",
                    "--project",
                    str(REAL_LABS),
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    "-c",
                    str(REAL_LABS / "pyproject.toml"),
                    "--rootdir",
                    tmp,
                    str(tests),
                ],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )

    def test_unmarked_test_is_a_usage_error_not_a_deselection(self) -> None:
        result = self.run_lab_pytest(
            "import pytest\n\n"
            "@pytest.mark.unit\ndef test_marked() -> None:\n    pass\n\n"
            "def test_unmarked() -> None:\n    pass\n"
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 4, output)
        self.assertIn("test_tiers.py::test_unmarked: tier markers []", output)
        self.assertNotIn("test_marked:", output)

    def test_two_tier_markers_are_a_usage_error(self) -> None:
        result = self.run_lab_pytest(
            "import pytest\n\n"
            "@pytest.mark.unit\n@pytest.mark.socket\ndef test_both() -> None:\n    pass\n"
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 4, output)
        self.assertIn("test_tiers.py::test_both: tier markers ['socket', 'unit']", output)

    def test_one_tier_marker_per_test_runs_every_test(self) -> None:
        result = self.run_lab_pytest(
            "import pytest\n\n"
            "@pytest.mark.unit\ndef test_unit() -> None:\n    pass\n\n"
            "@pytest.mark.socket\ndef test_socket() -> None:\n    pass\n"
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("2 passed", output)


if __name__ == "__main__":
    unittest.main()
