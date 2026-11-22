#!/usr/bin/env python3
"""Offline auditor for GitHub Actions release workflows.

It reads workflow files with a small built-in YAML-subset parser (no PyYAML, no network) and
applies the structural rules of the playbook "Audit and Harden a GitHub Actions Release Pipeline".
It checks what a file says. It cannot see repository settings, environment protection rules,
or what a cross-repository reusable workflow does inside.

Usage: audit_workflows.py [--fail-on SEVERITY] PATH...
Exit status: 0 if no finding at or above --fail-on (default high), 1 otherwise, 2 on a parse error.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

SEVERITIES = ("critical", "high", "medium", "low", "info")


class YamlError(ValueError):
    """Raised for YAML outside the supported subset, so a parse problem is loud, never silent."""


# --------------------------------------------------------------------------------------
# YAML subset parser: block maps, block lists, plain/quoted scalars, | and > scalars,
# flow lists and flow maps on one line. Anchors, aliases, tags, and multi-line plain or
# flow scalars are rejected.
# --------------------------------------------------------------------------------------


def _strip_comment(text: str) -> str:
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'" and (i == 0 or text[i - 1] in " \t:[{,("):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _split_key(content: str):
    """Return (key, rest) when content is 'key: rest' or 'key:', else None."""
    if content[:1] in ("[", "{", "|", ">"):
        return None
    if content[:1] in ('"', "'"):
        q = content[0]
        i = 1
        while i < len(content):
            if content[i] == "\\" and q == '"':
                i += 2
                continue
            if content[i] == q:
                if q == "'" and content[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                break
            i += 1
        else:
            return None
        rest = content[i + 1 :]
        if rest[:1] == ":" and (len(rest) == 1 or rest[1] in " \t"):
            return content[1:i], rest[1:].strip()
        return None
    m = re.search(r":(\s|$)", content)
    if not m:
        return None
    return content[: m.start()].strip(), content[m.end() :].strip()


def _split_top(text: str):
    items, depth, quote, cur = [], 0, "", []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    return [i.strip() for i in items if i.strip()]


def _scalar(text: str):
    s = text.strip()
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None
    if s[0] in "&*!":
        raise YamlError("anchors, aliases, and tags are not supported: " + s[:24])
    if s[0] == '"':
        if len(s) < 2 or s[-1] != '"':
            raise YamlError("unterminated or multi-line double-quoted scalar: " + s[:24])
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if s[0] == "'":
        if len(s) < 2 or s[-1] != "'":
            raise YamlError("unterminated or multi-line single-quoted scalar: " + s[:24])
        return s[1:-1].replace("''", "'")
    if s[0] == "[":
        if s[-1] != "]":
            raise YamlError("multi-line flow sequence is not supported: " + s[:24])
        return [_scalar(i) for i in _split_top(s[1:-1])]
    if s[0] == "{" and not s.startswith("${{"):
        if s[-1] != "}":
            raise YamlError("multi-line flow mapping is not supported: " + s[:24])
        out = {}
        for item in _split_top(s[1:-1]):
            kv = _split_key(item)
            if kv is None:
                raise YamlError("bad flow mapping entry: " + item)
            out[kv[0]] = _scalar(kv[1])
        return out
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


class _Parser:
    def __init__(self, text: str):
        self.lines = text.splitlines()
        self.i = 0

    def _peek(self):
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or stripped == "---":
                self.i += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if raw[indent : indent + 1] == "\t":
                raise YamlError("line %d: tab indentation is not valid YAML" % (self.i + 1))
            return indent, _strip_comment(stripped), self.i + 1
        return None

    def document(self):
        pk = self._peek()
        if pk is None:
            return None
        value = self._block(pk[0])
        extra = self._peek()
        if extra is not None:
            raise YamlError("line %d: unexpected content after the document body" % extra[2])
        return value

    def _block(self, indent: int):
        _, content, _ = self._peek()
        if content == "-" or content.startswith("- "):
            return self._seq(indent)
        return self._map(indent)

    def _seq(self, indent: int):
        out = []
        while True:
            pk = self._peek()
            if pk is None or pk[0] != indent or not (pk[1] == "-" or pk[1].startswith("- ")):
                return out
            _, content, _ = pk
            after = content[1:].lstrip()
            if after == "":
                self.i += 1
                nxt = self._peek()
                out.append(self._block(nxt[0]) if nxt and nxt[0] > indent else None)
                continue
            new_indent = indent + (len(content) - len(after))
            if after.startswith("- ") or _split_key(after) is not None:
                self.lines[self.i] = " " * new_indent + after
                out.append(self._block(new_indent))
            else:
                out.append(_scalar(after))
                self.i += 1

    def _block_scalar(self, parent_indent: int, header: str) -> str:
        style, chomp = header[0], ("-" if "-" in header[1:] else "")
        lines, block_indent = [], None
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if raw.strip() == "":
                lines.append("")
                self.i += 1
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if ind <= parent_indent:
                break
            if block_indent is None:
                block_indent = ind
            if ind < block_indent:
                break
            lines.append(raw[block_indent:])
            self.i += 1
        while lines and lines[-1] == "":
            lines.pop()
        text = "\n".join(lines) if style == "|" else " ".join(lines)
        return text if chomp else text + "\n"

    def _map(self, indent: int):
        out = {}
        while True:
            pk = self._peek()
            if pk is None or pk[0] < indent:
                return out
            ind, content, no = pk
            if ind > indent:
                raise YamlError("line %d: unexpected indentation" % no)
            if content == "-" or content.startswith("- "):
                return out
            kv = _split_key(content)
            if kv is None:
                raise YamlError("line %d: expected 'key: value', got %r" % (no, content[:40]))
            key, val = kv
            if key in out:
                raise YamlError("line %d: duplicate key %r" % (no, key))
            self.i += 1
            if val == "":
                nxt = self._peek()
                if nxt and nxt[0] > indent:
                    out[key] = self._block(nxt[0])
                elif nxt and nxt[0] == indent and (nxt[1] == "-" or nxt[1].startswith("- ")):
                    out[key] = self._seq(indent)
                else:
                    out[key] = None
            elif val[0] in "|>":
                out[key] = self._block_scalar(indent, val)
            else:
                out[key] = _scalar(val)


def parse_yaml(text: str):
    return _Parser(text).document()


# --------------------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------------------

SHA40 = re.compile(r"^[0-9a-f]{40}$")
PUBLISH_SCOPES = ("id-token", "attestations", "packages", "contents", "artifact-metadata", "deployments")
EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}", re.S)
# Untrusted event fields: the GitHub Security Lab list (2021-08), then two fields this lab adds.
UNTRUSTED = (
    "github.event.issue.title",
    "github.event.issue.body",
    "github.event.pull_request.title",
    "github.event.pull_request.body",
    "github.event.comment.body",
    "github.event.review.body",
    "github.event.pages.*.page_name",
    "github.event.commits.*.message",
    "github.event.head_commit.message",
    "github.event.head_commit.author.email",
    "github.event.head_commit.author.name",
    "github.event.commits.*.author.email",
    "github.event.commits.*.author.name",
    "github.event.pull_request.head.ref",
    "github.event.pull_request.head.label",
    "github.event.pull_request.head.repo.default_branch",
    "github.head_ref",
    "github.event.workflow_run.head_branch",
    "github.event.workflow_run.head_commit.message",
)
BUILD_CMD = re.compile(
    r"\b(make|cmake|ninja|npm (ci|install|run|test)|pnpm|yarn|pip install|python -m build|python -m pip|"
    r"poetry|uv (build|sync|pip)|cargo (build|test|install)|go (build|test|install)|mvn|gradle|bazel|"
    r"docker build|docker buildx|tox|pytest)\b"
)
PUBLISH_CMD = re.compile(r"(npm publish|twine upload|cargo publish|gem push|gh-action-pypi-publish)")
PR_HEAD = re.compile(
    r"(pull_request\.head|head_ref|refs/pull/|workflow_run\.head_(sha|branch|repository)|pull_request\.merge_commit)"
)


class Finding:
    def __init__(self, rule, severity, where, message):
        self.rule, self.severity, self.where, self.message = rule, severity, where, message


def _list(x):
    return [] if x is None else (x if isinstance(x, list) else [x])


def _events(wf):
    on = wf.get("on")
    if isinstance(on, dict):
        return set(on)
    return set(str(e) for e in _list(on))


def _perm_map(value):
    """Normalize a permissions value to ('read-all'|'write-all'|'map', dict)."""
    if value in ("read-all", "write-all"):
        return value, {}
    if isinstance(value, dict):
        return "map", {k: str(v) for k, v in value.items()}
    if value is None:
        return "map", {}
    return "map", {}


def _is_privileged(perms) -> bool:
    kind, scopes = perms
    if kind == "write-all":
        return True
    return any(scopes.get(s) == "write" for s in PUBLISH_SCOPES)


def _effective_perms(wf, job):
    if "permissions" in job:
        return _perm_map(job["permissions"])
    if "permissions" in wf:
        return _perm_map(wf["permissions"])
    return None  # repository default, unknown to a file-level audit


def _steps(job):
    return [s for s in _list(job.get("steps")) if isinstance(s, dict)]


def _uses_refs(wf):
    """Yield (where, ref) for step-level and job-level uses."""
    for jname, job in (wf.get("jobs") or {}).items():
        if "uses" in job:
            yield "jobs.%s.uses" % jname, str(job["uses"])
        for n, step in enumerate(_steps(job)):
            if "uses" in step:
                yield "jobs.%s.steps[%d]" % (jname, n), str(step["uses"])


def _is_action(step, name):
    return str(step.get("uses", "")).split("@")[0].lower().startswith(name)


def audit_workflow(wf) -> list:
    out = []
    jobs = wf.get("jobs") or {}
    events = _events(wf)

    # GHA001 token permissions
    if "permissions" not in wf:
        missing = [j for j, job in jobs.items() if "permissions" not in job]
        if missing:
            out.append(Finding("GHA001", "high", "workflow",
                               "no top-level permissions and no job-level permissions in %s: "
                               "they get the repository default token" % ", ".join(missing)))
        else:
            out.append(Finding("GHA001", "low", "workflow",
                               "no top-level permissions; every job sets its own, but a new job would "
                               "inherit the repository default"))
    else:
        kind, scopes = _perm_map(wf["permissions"])
        writes = sorted(k for k, v in scopes.items() if v == "write")
        if kind == "write-all" or writes:
            out.append(Finding("GHA001", "high", "workflow",
                               "top-level permissions grant write (%s): every job inherits it"
                               % ("write-all" if kind == "write-all" else ", ".join(writes))))
    for jname, job in jobs.items():
        if "permissions" in job and _perm_map(job["permissions"])[0] == "write-all":
            out.append(Finding("GHA001", "high", "jobs.%s" % jname, "permissions: write-all grants every scope"))

    # GHA002 unpinned uses
    for where, ref in _uses_refs(wf):
        if ref.startswith("./"):
            continue
        if ref.startswith("docker://"):
            if "@sha256:" not in ref:
                out.append(Finding("GHA002", "high", where, "container image %s is not pinned by digest" % ref))
            continue
        if "@" not in ref:
            out.append(Finding("GHA002", "high", where, "%s has no ref" % ref))
            continue
        name, _, rev = ref.rpartition("@")
        if not SHA40.match(rev):
            out.append(Finding("GHA002", "high", where, "%s is not pinned to a 40-hex commit SHA (ref %r is mutable)" % (name, rev)))

    # GHA003 dangerous checkout, GHA014 dangerous trigger
    danger = events & {"pull_request_target", "workflow_run"}
    for jname, job in jobs.items():
        for n, step in enumerate(_steps(job)):
            if danger and _is_action(step, "actions/checkout"):
                w = step.get("with") or {}
                probe = " ".join(str(w.get(k, "")) for k in ("ref", "repository"))
                if PR_HEAD.search(probe) or w.get("allow-unsafe-pr-checkout") is True:
                    out.append(Finding("GHA003", "critical", "jobs.%s.steps[%d]" % (jname, n),
                                       "%s workflow checks out untrusted pull request code (%s)"
                                       % ("/".join(sorted(danger)), probe.strip() or "allow-unsafe-pr-checkout")))
    if danger and not any(f.rule == "GHA003" for f in out):
        out.append(Finding("GHA014", "low", "workflow",
                           "privileged trigger %s: confirm no step executes pull request code" % ", ".join(sorted(danger))))

    # GHA004 template injection
    for jname, job in jobs.items():
        for n, step in enumerate(_steps(job)):
            where = "jobs.%s.steps[%d]" % (jname, n)
            bodies = []
            if isinstance(step.get("run"), str):
                bodies.append(step["run"])
            if _is_action(step, "actions/github-script") and isinstance((step.get("with") or {}).get("script"), str):
                bodies.append(step["with"]["script"])
            for body in bodies:
                for expr in EXPR.findall(body):
                    token = re.match(r"[A-Za-z0-9_.*\[\]-]+", expr)
                    token = re.sub(r"\[[^\]]*\]", ".*", token.group(0)) if token else ""
                    if any(fnmatch.fnmatchcase(token, pat) for pat in UNTRUSTED):
                        out.append(Finding("GHA004", "high", where,
                                           "untrusted ${{ %s }} is interpolated into a script body" % token))

    # GHA005 persisted checkout credentials
    for jname, job in jobs.items():
        for n, step in enumerate(_steps(job)):
            if _is_action(step, "actions/checkout"):
                pc = (step.get("with") or {}).get("persist-credentials")
                if pc is not False and str(pc).lower() != "false":
                    out.append(Finding("GHA005", "medium", "jobs.%s.steps[%d]" % (jname, n),
                                       "checkout leaves the token on disk (persist-credentials is not false)"))

    # GHA006 secret scope
    for key in ("env",):
        for name, value in (wf.get(key) or {}).items():
            if "secrets." in str(value):
                out.append(Finding("GHA006", "high", "workflow.env.%s" % name,
                                   "secret exposed to every step of every job through workflow-level env"))
    for jname, job in jobs.items():
        for name, value in (job.get("env") or {}).items():
            if "secrets." in str(value):
                out.append(Finding("GHA006", "medium", "jobs.%s.env.%s" % (jname, name),
                                   "secret exposed to every step of the job through job-level env"))
        if str(job.get("secrets", "")).strip() == "inherit":
            out.append(Finding("GHA006", "high", "jobs.%s.secrets" % jname, "secrets: inherit passes every secret to the callee"))

    # GHA007 build and publish credentials in one job, GHA013 self-hosted release runners
    for jname, job in jobs.items():
        perms = _effective_perms(wf, job)
        privileged = perms is not None and _is_privileged(perms)
        builds = [n for n, s in enumerate(_steps(job))
                  if _is_action(s, "actions/checkout") or BUILD_CMD.search(str(s.get("run", "")))]
        if privileged and builds:
            out.append(Finding("GHA007", "high", "jobs.%s" % jname,
                               "job holds publish-class permissions and also checks out or builds code (steps %s)"
                               % ",".join(str(b) for b in builds)))
        if privileged and "self-hosted" in [str(x) for x in _list(job.get("runs-on"))]:
            out.append(Finding("GHA013", "high", "jobs.%s" % jname, "privileged job runs on a self-hosted runner"))

    # GHA008 caches inside privileged jobs
    for jname, job in jobs.items():
        perms = _effective_perms(wf, job)
        if perms is None or not _is_privileged(perms):
            continue
        for n, step in enumerate(_steps(job)):
            w = step.get("with") or {}
            cached = _is_action(step, "actions/cache") or (
                "cache" in w and str(w["cache"]).lower() not in ("false", "")) or str(w.get("cache-mode", "")).startswith("write")
            if cached:
                out.append(Finding("GHA008", "high", "jobs.%s.steps[%d]" % (jname, n),
                                   "cache restore or save inside a job with publish-class permissions"))

    # GHA009 environment gate on publishing jobs
    for jname, job in jobs.items():
        if "uses" in job:
            continue  # reusable-workflow call: the callee owns its jobs and environments
        perms = _effective_perms(wf, job)
        publishes = perms is not None and (
            perms[0] == "write-all"
            or (perms[1].get("id-token") == "write" and not _is_attest_only(job))
            or perms[1].get("packages") == "write"
            or perms[1].get("contents") == "write")
        if publishes and "environment" not in job:
            out.append(Finding("GHA009", "medium", "jobs.%s" % jname,
                               "publishing job has no environment, so no required-reviewer or branch-rule gate"))

    # GHA010 hidden files in artifacts
    for jname, job in jobs.items():
        for n, step in enumerate(_steps(job)):
            if _is_action(step, "actions/upload-artifact"):
                w = step.get("with") or {}
                if w.get("include-hidden-files") is True:
                    wide = str(w.get("path", "")).strip() in (".", "./", "")
                    out.append(Finding("GHA010", "high" if wide else "medium", "jobs.%s.steps[%d]" % (jname, n),
                                       "artifact includes hidden files%s" % (" from the whole workspace (path %r)" % w.get("path") if wide else "")))

    # GHA011 long-lived registry secret in a publishing step
    for jname, job in jobs.items():
        for n, step in enumerate(_steps(job)):
            blob = " ".join([str(step.get("run", "")), str(step.get("uses", "")), str(step.get("with", "")), str(step.get("env", ""))])
            if PUBLISH_CMD.search(blob):
                for secret in re.findall(r"secrets\.([A-Za-z0-9_]+)", blob):
                    if secret != "GITHUB_TOKEN":
                        out.append(Finding("GHA011", "medium", "jobs.%s.steps[%d]" % (jname, n),
                                           "publishing with long-lived secret %s: use trusted publishing (OIDC) where the registry supports it" % secret))

    # GHA012 provenance emitted and permitted
    on = wf.get("on")
    push = on.get("push") if isinstance(on, dict) else None
    releases = "release" in events or (isinstance(push, dict) and "tags" in push)
    emitters = []
    for jname, job in jobs.items():
        uses = str(job.get("uses", "")).lower()
        if "attest" in uses or "provenance" in uses or "slsa-github-generator" in uses:
            emitters.append(jname)
        for step in _steps(job):
            if _is_action(step, "actions/attest") or _is_action(step, "actions/attest-build-provenance"):
                emitters.append(jname)
                perms = _effective_perms(wf, job)
                ok = perms is not None and (perms[0] == "write-all" or (
                    perms[1].get("id-token") == "write" and perms[1].get("attestations") == "write"))
                if not ok:
                    out.append(Finding("GHA012", "high", "jobs.%s" % jname,
                                       "attest step without id-token: write and attestations: write in effect"))
    if releases and not emitters:
        out.append(Finding("GHA012", "medium", "workflow", "release workflow emits no provenance attestation"))
    return out


def _is_attest_only(job) -> bool:
    """True when every step is an artifact download or an attest action (no repo code runs)."""
    steps = _steps(job)
    return bool(steps) and all(
        _is_action(s, "actions/download-artifact") or _is_action(s, "actions/attest") for s in steps)


def audit_text(text: str) -> list:
    wf = parse_yaml(text)
    if not isinstance(wf, dict) or "jobs" not in wf:
        raise YamlError("not a workflow: no top-level jobs mapping")
    return audit_workflow(wf)


def _iter_files(paths):
    for p in paths:
        p = Path(p)
        if p.is_dir():
            yield from sorted(x for x in p.rglob("*") if x.suffix in (".yml", ".yaml"))
        else:
            yield p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--fail-on", choices=SEVERITIES, default="high")
    ap.add_argument("--root", default=".", help="print file names relative to this directory")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    threshold = SEVERITIES.index(args.fail_on)
    counts = {s: 0 for s in SEVERITIES}
    failed = False
    for path in _iter_files(args.paths):
        name = str(path.resolve().relative_to(root)) if root in path.resolve().parents else path.name
        try:
            findings = audit_text(path.read_text())
        except YamlError as exc:
            print("%s: PARSE ERROR: %s" % (name, exc))
            return 2
        findings.sort(key=lambda f: (SEVERITIES.index(f.severity), f.rule, f.where))
        print("== %s: %d finding(s)" % (name, len(findings)))
        for f in findings:
            counts[f.severity] += 1
            print("  %-8s %s %-26s %s" % (f.severity.upper(), f.rule, f.where, f.message))
            if SEVERITIES.index(f.severity) <= threshold:
                failed = True
    print("summary: " + ", ".join("%s=%d" % (s, counts[s]) for s in SEVERITIES))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
