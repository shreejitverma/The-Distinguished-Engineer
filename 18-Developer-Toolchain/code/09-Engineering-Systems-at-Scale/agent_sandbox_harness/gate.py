#!/usr/bin/env python3
"""Deterministic ship gate for the agent_sandbox_harness lab.

Modes (all print TOON-style structured output on stdout; exit 0 pass, 1 reject, 2 usage error):
  check   --repo DIR --base SHA --new SHA --ref REF   run the five gate checks on a candidate
  hook                                                 pre-receive hook: read "old new ref" lines from stdin
  attest  --key FILE --ref REF --sha SHA               print the HMAC attestation for a green candidate
  origin-hook --key FILE                               origin-side pre-receive: accept only attested updates
"""

import argparse
import hmac
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

ZERO = "0" * 40
AGENT_PREFIX = "refs/heads/agent/"
PROTECTED_PREFIXES = (".gate/", ".github/")
DIFF_BUDGET = 200
SECRET_PATTERNS = (
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def git(*args, repo=".", check=True):
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


def extract_tree(repo, sha, dest):
    archive = subprocess.Popen(
        ["git", "-C", repo, "archive", "--format=tar", sha], stdout=subprocess.PIPE
    )
    untar = subprocess.run(["tar", "-x", "-C", dest], stdin=archive.stdout, check=False)
    archive.stdout.close()
    if archive.wait() != 0 or untar.returncode != 0:
        raise RuntimeError("could not extract %s" % sha)


def run_tests(repo, sha):
    """Return (tests_ran, exit_code) for the project's unittest suite at sha."""
    scratch = tempfile.mkdtemp(prefix="gate-tree-")
    try:
        extract_tree(repo, sha, scratch)
        if not os.path.isdir(os.path.join(scratch, "tests")):
            return 0, 5
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"],
            cwd=scratch, env=env, capture_output=True, text=True, timeout=60, check=False,
        )
        match = re.search(r"Ran (\d+) tests?", proc.stderr)
        return (int(match.group(1)) if match else 0), proc.returncode
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def check_ref_policy(ref):
    if ref.startswith(AGENT_PREFIX):
        return "pass", "agent branch"
    return "fail", "agents may only update refs/heads/agent/*"


def check_protected_paths(repo, base, new):
    bad = []
    for line in git("diff", "--name-status", "--no-renames", base, new, repo=repo).splitlines():
        status, path = line.split("\t", 1)
        if path.startswith(PROTECTED_PREFIXES):
            bad.append("%s %s" % ("touched", path))
        elif status == "D" and path.startswith("tests/"):
            bad.append("%s %s" % ("deleted", path))
    if bad:
        return "fail", "; ".join(sorted(bad))
    return "pass", "none"


def check_secrets(repo, base, new):
    findings = []
    path = "?"
    for line in git("diff", "-U0", "--no-renames", base, new, repo=repo).splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append("%s in %s" % (label, path))
    if findings:
        return "fail", "%d finding(s): %s" % (len(findings), "; ".join(sorted(set(findings))))
    return "pass", "0 findings"


def check_diff_budget(repo, base, new):
    total = 0
    for line in git("diff", "--numstat", "--no-renames", base, new, repo=repo).splitlines():
        added, deleted, _ = line.split("\t", 2)
        if added.isdigit() and deleted.isdigit():
            total += int(added) + int(deleted)
    status = "pass" if total <= DIFF_BUDGET else "fail"
    return status, "%d of %d changed lines" % (total, DIFF_BUDGET)


def check_tests(repo, base, new):
    base_ran, _ = run_tests(repo, base)
    ran, code = run_tests(repo, new)
    if ran < base_ran:
        return "fail", "ran %d tests, base ran %d (test-count ratchet)" % (ran, base_ran)
    if code != 0:
        return "fail", "ran %d tests, exit code %d" % (ran, code)
    return "pass", "ran %d tests, base ran %d" % (ran, base_ran)


def evaluate(repo, base, new, ref):
    results = [("ref_policy",) + check_ref_policy(ref)]
    results.append(("protected_paths",) + check_protected_paths(repo, base, new))
    results.append(("secrets",) + check_secrets(repo, base, new))
    results.append(("diff_budget",) + check_diff_budget(repo, base, new))
    results.append(("tests",) + check_tests(repo, base, new))
    return results


def render(ref, results):
    passed = sum(1 for _, status, _ in results if status == "pass")
    green = passed == len(results)
    out = ["result: %s" % ("green" if green else "rejected"), "ref: %s" % ref]
    out.append("checks[%d]{name,status,detail}:" % len(results))
    for name, status, detail in results:
        out.append('  %s,%s,"%s"' % (name, status, detail))
    out.append("passed: %d of %d" % (passed, len(results)))
    if not green:
        out.append("help[1]:")
        out.append("  Fix the failing checks and push again; the gate never edits your branch")
    return green, "\n".join(out)


def attestation(key_file, ref, sha):
    with open(key_file, "rb") as handle:
        key = handle.read().strip()
    return hmac.new(key, ("%s\n%s" % (ref, sha)).encode("ascii"), hashlib.sha256).hexdigest()


def cmd_check(args):
    green, text = render(args.ref, evaluate(args.repo, args.base, args.new, args.ref))
    print(text)
    return 0 if green else 1


def cmd_hook(_args):
    exit_code = 0
    for raw in sys.stdin.read().splitlines():
        old, new, ref = raw.split()
        if new == ZERO:
            status, detail = check_ref_policy(ref)
            print("result: %s\nref: %s\ndetail: \"delete %s\"" % ("green" if status == "pass" else "rejected", ref, detail))
            exit_code |= 0 if status == "pass" else 1
            continue
        base = git("merge-base", "refs/heads/main", new).strip()
        green, text = render(ref, evaluate(".", base, new, ref))
        print(text)
        exit_code |= 0 if green else 1
    return exit_code


def cmd_attest(args):
    print(attestation(args.key, args.ref, args.sha))
    return 0


def cmd_origin_hook(args):
    count = int(os.environ.get("GIT_PUSH_OPTION_COUNT", "0"))
    options = [os.environ.get("GIT_PUSH_OPTION_%d" % i, "") for i in range(count)]
    presented = next((o[len("attest="):] for o in options if o.startswith("attest=")), "")
    exit_code = 0
    for raw in sys.stdin.read().splitlines():
        old, new, ref = raw.split()
        if new == ZERO:
            continue
        expected = attestation(args.key, ref, new)
        if not hmac.compare_digest(presented, expected):
            print("origin: refused %s: no valid gate attestation" % ref)
            exit_code = 1
    return exit_code


def build_parser():
    parser = Parser(prog="gate.py", description="Deterministic ship gate")
    sub = parser.add_subparsers(dest="command", parser_class=Parser)
    check = sub.add_parser("check")
    check.add_argument("--repo", default=".")
    check.add_argument("--base", required=True)
    check.add_argument("--new", required=True)
    check.add_argument("--ref", required=True)
    check.set_defaults(func=cmd_check)
    sub.add_parser("hook").set_defaults(func=cmd_hook)
    attest = sub.add_parser("attest")
    attest.add_argument("--key", required=True)
    attest.add_argument("--ref", required=True)
    attest.add_argument("--sha", required=True)
    attest.set_defaults(func=cmd_attest)
    origin = sub.add_parser("origin-hook")
    origin.add_argument("--key", required=True)
    origin.set_defaults(func=cmd_origin_hook)
    return parser


def main(argv):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if not getattr(args, "func", None):
            raise UsageError("a command is required")
    except UsageError as exc:
        print("error: %s" % exc)
        print("code: VALIDATION_ERROR")
        print("help[1]:")
        print("  Run `gate.py check --repo DIR --base SHA --new SHA --ref REF`")
        return 2
    try:
        return args.func(args)
    except RuntimeError as exc:
        print("error: %s" % exc)
        print("code: GATE_ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
