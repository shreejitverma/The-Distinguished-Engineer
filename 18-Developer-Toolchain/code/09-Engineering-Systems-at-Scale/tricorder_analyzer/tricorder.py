"""A toy Tricorder: staged analyzers, notes with ordered fixes, review-time filtering,
fingerprint baselines, a validated fix applier, feedback policy, and SARIF export.

The shapes follow the Tricorder paper (Sadowski et al., ICSE 2015): an analyzer has a
category and a stage, and Analyze returns Notes with a location, a message, a URL and an
ordered list of fixes.
Offsets are byte offsets into ASCII source, so character and byte offsets coincide.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass

MACHINE = "machine-applicable"
MAYBE = "maybe-incorrect"
FILES, DEPS, COMPILATION = "FILES", "DEPS", "COMPILATION"


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Fix:
    description: str
    edits: tuple
    applicability: str = MACHINE
    ast_preserving: bool = False


@dataclass(frozen=True)
class Note:
    category: str
    path: str
    line: int
    col: int
    message: str
    url: str
    line_text: str
    fixes: tuple = ()


class Analyzer:
    category = "Base"
    stage = FILES
    correctness_only = False

    def analyze(self, path, text, tree):
        raise NotImplementedError


class Source:
    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")
        self.starts = []
        pos = 0
        for line in self.lines:
            self.starts.append(pos)
            pos += len(line) + 1

    def offset(self, line, col):
        return self.starts[line - 1] + col

    def line_text(self, line):
        return self.lines[line - 1] if 1 <= line <= len(self.lines) else ""


def analyze_file(path, text, analyzers):
    """Run every analyzer whose stage has its inputs. Return (notes, notices).

    FILES analyzers need only the text.
    COMPILATION analyzers need a parse; a file that does not parse yields a notice
    for that stage and no notes from it, and never stops the FILES stage.
    """
    notes, notices = [], []
    tree = None
    parse_error = None
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        parse_error = exc
    for analyzer in analyzers:
        if analyzer.stage == FILES:
            notes.extend(analyzer.analyze(path, text, None))
        elif analyzer.stage == COMPILATION:
            if tree is None:
                notices.append("%s: skipped %s (parse failed: %s)" % (path, analyzer.category, parse_error.msg))
            else:
                notes.extend(analyzer.analyze(path, text, tree))
    return sorted(notes, key=lambda n: (n.path, n.line, n.col, n.category)), notices


def fingerprint(note, occurrence):
    """Content-keyed identity that survives line shifts (SARIF partialFingerprints idea)."""
    raw = "%s\0%s\0%s" % (note.category, note.line_text.strip(), occurrence)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()[:12]


def fingerprints(notes):
    seen = {}
    out = []
    for note in notes:
        key = (note.path, note.category, note.line_text.strip())
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        out.append((fingerprint(note, occurrence), note))
    return out


def changed_lines(hunks):
    """Read the new-side ranges from one file's zero-context unified diff headers."""
    lines = set()
    for header in hunks:
        match = re.fullmatch(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@(?: .*)?", header)
        if match is None:
            raise ValueError("invalid unified diff hunk header: %r" % header)
        start, count = match.groups()
        start, count = int(start), int(count) if count is not None else 1
        if count and not start:
            raise ValueError("nonempty new-side hunk starts at line zero")
        lines.update(range(start, start + count))
    return lines


def review_view(notes, lines):
    return [n for n in notes if n.line in lines]


def baseline_states(old_notes, new_notes):
    """SARIF baselineState: new / unchanged for current results, absent for vanished ones."""
    old_fp = {(note.path, fp) for fp, note in fingerprints(old_notes)}
    new_fp = fingerprints(new_notes)
    states = [("unchanged" if (n.path, fp) in old_fp else "new", n) for fp, n in new_fp]
    new_set = {(note.path, fp) for fp, note in new_fp}
    absent = [("absent", n) for fp, n in fingerprints(old_notes) if (n.path, fp) not in new_set]
    return states, absent


def _overlap(a, b):
    return a.start < b.end and b.start < a.end


def apply_edits(text, edits):
    """Apply non-overlapping edits. Equal-point insertions keep input order."""
    indexed = sorted(enumerate(edits), key=lambda p: (p[1].start, p[1].end, p[0]), reverse=True)
    for _, e in indexed:
        text = text[: e.start] + e.text + text[e.end:]
    return text


def validate_python(before_tree, after, ast_preserving):
    """Syntax must survive; style fixes must also keep the AST identical."""
    try:
        tree_after = ast.parse(after)
    except SyntaxError as exc:
        return False, "breaks syntax: %s" % exc.msg
    if ast_preserving and ast.dump(before_tree) != ast.dump(tree_after):
        return False, "changes the AST but is declared style-only"
    return True, "ok"


def apply_fixes(text, fixes, *, python_source=True):
    """Greedy, atomic per fix. Returns (new_text, applied, rejected) with reasons.

    Fixes are independent changes against the same baseline text, like git commits with one
    parent (Go analysis.Diagnostic documentation), so a fix whose edits overlap an already
    accepted fix is rejected whole and left for a later pass.
    """
    accepted, applied, rejected = [], [], []
    valid_ranges = []
    for fix in fixes:
        own = sorted(fix.edits, key=lambda e: (e.start, e.end))
        if not own or any(not 0 <= e.start <= e.end <= len(text) for e in own):
            rejected.append((fix, "malformed: empty or out-of-bounds edit set"))
        elif any(_overlap(a, b) for a, b in zip(own, own[1:])):
            rejected.append((fix, "malformed: edits overlap each other"))
        else:
            valid_ranges.append((own[0].start, fix))
    try:
        before_tree = ast.parse(text) if python_source and valid_ranges else None
    except SyntaxError:
        return text, [], rejected + [(fix, "pre-image does not parse") for _, fix in valid_ranges]

    for _, fix in sorted(valid_ranges, key=lambda entry: entry[0]):
        if any(_overlap(e, a) for e in fix.edits for a in accepted):
            rejected.append((fix, "conflicts with an accepted fix"))
            continue
        if python_source:
            ok, why = validate_python(before_tree, apply_edits(text, fix.edits), fix.ast_preserving)
            if ok and accepted:
                composed = apply_edits(text, (*accepted, *fix.edits))
                ok, why = validate_python(before_tree, composed, fix.ast_preserving and all(f.ast_preserving for f in applied))
            if not ok:
                rejected.append((fix, why))
                continue
        accepted.extend(fix.edits)
        applied.append(fix)
    return apply_edits(text, accepted), applied, rejected


def plan(notes, allow=(MACHINE,)):
    """First fix of each note whose applicability is allowed (the ordered list is a preference)."""
    chosen = []
    for note in notes:
        for fix in note.fixes:
            if fix.applicability in allow:
                chosen.append(fix)
                break
    return chosen


def autofix(path, text, analyzers, allow=(MACHINE,), max_passes=10):
    """Re-run analysis after each pass, as ESLint does, until a pass applies nothing."""
    log = []
    for n in range(1, max_passes + 1):
        notes, _ = analyze_file(path, text, analyzers)
        new_text, applied, rejected = apply_fixes(text, plan(notes, allow))
        log.append((n, len(notes), len(applied), len(rejected)))
        if not applied:
            return text, log
        text = new_text
    return text, log


def not_useful_rate(not_useful, please_fix, apply_fix):
    """Tricorder paper section IV-E: NOT_USEFUL / (NOT_USEFUL + PLEASE_FIX + APPLY_FIX)."""
    denom = not_useful + please_fix + apply_fix
    return not_useful / denom if denom else None


def disposition(rate):
    """Thresholds from the paper: >= 10% probation, > 25% may be turned off at once."""
    if rate is None:
        return "no-signal"
    if rate > 0.25:
        return "disable-candidate"
    if rate >= 0.10:
        return "probation"
    return "healthy"


def to_sarif(notes_with_fp):
    """A minimal SARIF 2.1.0 log: results, partialFingerprints, and fixes with replacements."""
    results = []
    for fp, note in notes_with_fp:
        result = {
            "ruleId": note.category,
            "level": "warning",
            "message": {"text": note.message},
            "partialFingerprints": {"toyTricorderLine/v1": fp},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": note.path},
                "region": {"startLine": note.line, "startColumn": note.col + 1}}}],
        }
        fixes = []
        for fix in note.fixes:
            fixes.append({
                "description": {"text": fix.description},
                "artifactChanges": [{
                    "artifactLocation": {"uri": note.path},
                    "replacements": [{
                        "deletedRegion": {"charOffset": e.start, "charLength": e.end - e.start},
                        "insertedContent": {"text": e.text}} for e in fix.edits]}],
                "properties": {"applicability": fix.applicability}})
        if fixes:
            result["fixes"] = fixes
        results.append(result)
    return {"version": "2.1.0", "runs": [{
        "tool": {"driver": {"name": "toy-tricorder"}},
        "columnKind": "unicodeCodePoints",
        "results": results,
    }]}


def dumps(obj):
    return json.dumps(obj, indent=2, sort_keys=True)
