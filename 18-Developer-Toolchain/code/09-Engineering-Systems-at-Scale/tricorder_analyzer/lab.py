"""Tricorder lab: each section proves one mechanism and would print differently without it."""
from __future__ import annotations

import ast
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import analyzers as A
from tricorder import (MACHINE, MAYBE, Edit, Fix, analyze_file, apply_fixes, autofix, baseline_states,
                       changed_lines, disposition, dumps, fingerprints, not_useful_rate, plan, review_view,
                       to_sarif)

OLD = '''import os
import json

LIMIT = 100


def load(path):
    with open(path) as f:
        return json.load(f)


def total(items, cache=[]):
    cache.append(len(items))
    return sum(items)
'''

NEW = '''import os
import json
import hashlib

LIMIT = 100


def load(path):
    with open(path) as f:
        return f.read()


def total(items, cache=[]):
    cache.append(len(items))
    return sum(items)


def check(value, tag):
    if value == None:
        ValueError("missing value for " + tag)
    return value
'''.replace("== None:\n", "== None:   \n")

REPORTS = "import billing\n\n\ndef show():\n    return billing.LIMIT\n"
HUNKS = ("@@ -2,0 +3 @@", "@@ -9 +10 @@", "@@ -14,0 +16,6 @@")


def check(cond, label):
    if not cond:
        raise AssertionError(label)


def cats(notes):
    out = {}
    for n in notes:
        out[n.category] = out.get(n.category, 0) + 1
    return ", ".join("%s=%d" % kv for kv in sorted(out.items())) or "none"


def head(title):
    print("\n== %s ==" % title)


def worker(job):
    path, text = job
    notes, _ = analyze_file(path, text, A.build())
    return notes


def main():
    all_an = A.build()

    head("1. Stages: a parse failure costs the COMPILATION stage only")
    broken = 'BAD = "x"   \ndef broken(:\n    pass\n'
    notes, notices = analyze_file("broken.py", broken, all_an)
    print("notes from FILES stage: %s" % cats(notes))
    for n in notices:
        print("notice: %s" % n.split(" (")[0])
    check(len(notes) == 1 and len(notices) == 4, "stage isolation")

    head("2. Where results appear: dashboard, review view, baseline")
    old_notes, _ = analyze_file("billing.py", OLD, all_an)
    new_notes, _ = analyze_file("billing.py", NEW, all_an)
    lines = changed_lines(HUNKS)
    check(lines == {3, 10, 16, 17, 18, 19, 20, 21}, "read new-side hunk coordinates")
    check(changed_lines(("@@ -1 +0,0 @@",)) == set(), "a deletion creates no new-side lines")
    shown = review_view(new_notes, lines)
    print("old file notes:      %s" % cats(old_notes))
    print("dashboard (all new): %s" % cats(new_notes))
    print("changed lines:       %s" % sorted(lines))
    print("review view:         %s" % cats(shown))
    states, absent = baseline_states(old_notes, new_notes)
    print("fingerprint baseline: new=%d unchanged=%d absent=%d" % (
        sum(s == "new" for s, _ in states), sum(s == "unchanged" for s, _ in states), len(absent)))
    for s, n in states:
        if s == "new":
            print("  new: line %d %s" % (n.line, n.category))
    old_keys = {(n.category, n.line) for n in old_notes}
    line_keyed_new = [n for n in new_notes if (n.category, n.line) not in old_keys]
    print("line-keyed baseline would call %d of %d results new" % (len(line_keyed_new), len(new_notes)))
    json_note = [n for n in new_notes if n.category == "UnusedImport" and "json" in n.message][0]
    check(json_note.line not in lines, "json import is an unchanged line")
    check(json_note not in shown, "line filter misses the import orphaned by the change")
    check(("new", json_note) in states, "baseline catches the orphaned import")
    check(len(line_keyed_new) > sum(s == "new" for s, _ in states), "line keys are unstable under shifts")
    moved = replace(json_note, path="other.py")
    check(baseline_states([json_note], [moved]) == ([('new', moved)], [('absent', json_note)]),
          "separate artifacts can share a content fingerprint without sharing baseline identity")
    affected = A.affected_modules({"billing.py"}, {"reports.py": REPORTS, "other.py": "import json\n"})
    check(affected == ["reports.py"], "DEPS includes importers rather than the entire corpus")
    print("affected by billing.py (DEPS stage): %s" % affected)

    head("3. Applicability gates what an automatic pass may do")
    safe_text, _ = autofix("billing.py", NEW, all_an, allow=(MACHINE,))
    risky_text, _ = autofix("billing.py", NEW, all_an, allow=(MACHINE, MAYBE))
    check("== None" in safe_text and "is None" in risky_text, "maybe-incorrect fix held back by default")
    print("machine-only pass leaves '== None': %s" % ("== None" in safe_text))
    print("with maybe-incorrect it becomes 'is None': %s" % ("is None" in risky_text))

    class AlwaysEqual:
        def __eq__(self, other):
            return True

    before = eval("obj == None", {"obj": AlwaysEqual()})
    after = eval("obj is None", {"obj": AlwaysEqual()})
    print("obj == None -> %s ; obj is None -> %s" % (before, after))
    check(before != after, "the two forms differ for an overloaded __eq__")

    head("4. Fix composition: minimal disjoint edits (SARIF Appendix G)")
    xml = "<lineItem partNumber=A3101 />"
    s = xml.index("A3101")
    e = s + 5
    quote_coarse = Fix("Quote value", (Edit(s, e, '"A3101"'),))
    quote_minimal = Fix("Quote value", (Edit(s, s, '"'), Edit(e, e, '"')))
    renumber = Fix("Renumber", (Edit(s, e, "AA3101"),))
    for label, fixes in (("coarse", [quote_coarse, renumber]), ("minimal", [quote_minimal, renumber])):
        out, applied, rejected = apply_fixes(xml, fixes, python_source=False)
        print("%-8s applied=%d rejected=%d -> %s" % (label, len(applied), len(rejected), out))
        for _, why in rejected:
            print("         rejected: %s" % why)
    out, _, _ = apply_fixes(xml, [quote_minimal, renumber], python_source=False)
    check(out == '<lineItem partNumber="AA3101" />', "minimal edits compose")
    check(len(apply_fixes(xml, [quote_coarse, renumber], python_source=False)[2]) == 1, "coarse edits conflict")

    head("5. Validation: a fix must not break the file it fixes")
    naive_src = "DEBUG = True\nif DEBUG:\n    import pdb\n"
    for guard in (False, True):
        an = [A.UnusedImport(guard_sole_statement=guard)]
        ns, _ = analyze_file("m.py", naive_src, an)
        out, applied, rejected = apply_fixes(naive_src, plan(ns))
        print("sole-statement guard=%-5s applied=%d rejected=%d parses=%s" % (
            guard, len(applied), len(rejected), ast.parse(out) is not None))
        for _, why in rejected:
            print("   rejected: %s" % why)
        expected = "DEBUG = True\nif DEBUG:\n    pass\n" if guard else naive_src
        check(len(rejected) == (0 if guard else 1) and out == expected,
              "naive deletion must be rejected, guarded must apply")
    doc_src = 'def f():\n    """Return a value.   \n    More.\n    """\n    return 1   \n'
    ns, _ = analyze_file("d.py", doc_src, A.build(["TrailingWhitespace"]))
    out, applied, rejected = apply_fixes(doc_src, plan(ns))
    print("trailing whitespace: notes=%d applied=%d rejected=%d" % (len(ns), len(applied), len(rejected)))
    for _, why in rejected:
        print("   rejected: %s" % why)
    check(len(applied) == 1 and len(rejected) == 1, "docstring whitespace is data, not style")
    check("value.   \n" in out, "docstring left intact")
    compose_src = "def f():\n    value = item\n"
    start, middle = compose_src.index("value"), compose_src.index("item")
    return_fix = Fix("Return item", (Edit(start, middle, "return "),))
    yield_fix = Fix("Yield sent", (Edit(middle, middle + len("item"), "yield sent"),))
    check(apply_fixes(compose_src, [yield_fix])[1] == [yield_fix], "yield fix parses by itself")
    out, applied, rejected = apply_fixes(compose_src, [return_fix, yield_fix])
    check(out == "def f():\n    return item\n" and applied == [return_fix] and rejected[0][0] == yield_fix,
          "disjoint fixes can compose into invalid Python")
    print("disjoint edits, invalid combination: applied=%d rejected=%d" % (len(applied), len(rejected)))
    check(apply_fixes("if :\n", [Fix("Change invalid input", (Edit(0, 2, "while"),))])[2],
          "an invalid pre-image blocks automatic Python fixes")

    head("6. Passes: conflicts defer, then the pass reaches a fixed point")
    conflict_src = "import os   \n\n\ndef f():\n    return 1\n"
    out, log = autofix("c.py", conflict_src, all_an)
    for n, found, applied, rej in log:
        print("pass %d: notes=%d applied=%d rejected=%d" % (n, found, applied, rej))
    check(log[0][3] == 1 and out == "\n\ndef f():\n    return 1\n", "overlap rejected, line deleted")
    final, log = autofix("billing.py", NEW, all_an)
    for n, found, applied, rej in log:
        print("billing pass %d: notes=%d applied=%d rejected=%d" % (n, found, applied, rej))
    again, log2 = autofix("billing.py", final, all_an)
    print("second run: passes=%d changed=%s" % (len(log2), again != final))
    check(again == final and len(log2) == 1, "idempotent at the fixed point")
    left, _ = analyze_file("billing.py", final, all_an)
    print("left for a human: %s" % cats(left))

    head("7. Can this check become a compiler error? (cleanup rehearsal)")
    corpus = {
        "a.py": "def f(x):\n    if x:\n        ValueError('bad')\n    return x\n",
        "b.py": "class EmptyInput(ValueError):\n    pass\n\ndef g(items=[]):\n    EmptyInput('empty')\n    return items\n",
        "c.py": "import sys\n\nif 1 == None:\n    pass\n",
    }
    expected_counts = {"DeadException": (2, 0), "MutableDefaultArgument": (1, 1),
                       "UnusedImport": (1, 0), "NoneComparison": (1, 1)}
    dead_only = A.build(["DeadException"])
    shadowed = "def f(ValueError):\n    ValueError('ordinary call')\n"
    check(not analyze_file("shadowed.py", shadowed, dead_only)[0], "skip a parameter that shadows a builtin")
    check(len(analyze_file("b.py", corpus["b.py"], dead_only)[0]) == 1, "recognize a module-local exception")
    for name, expected in expected_counts.items():
        an = A.REGISTRY[name]()
        before_count = sum(len(analyze_file(p, t, [an])[0]) for p, t in corpus.items())
        remaining = 0
        for p, t in corpus.items():
            fixed, _ = autofix(p, t, [an], allow=(MACHINE,))
            remaining += len(analyze_file(p, fixed, [an])[0])
        reasons = []
        if not an.correctness_only:
            reasons.append("not correctness-only")
        if remaining:
            reasons.append("%d of %d instances survive mechanical cleanup" % (remaining, before_count))
        print("%-22s instances=%d remaining=%d -> %s" % (
            name, before_count, remaining, "ENABLE AS ERROR" if not reasons else "review-time only: " + "; ".join(reasons)))
        check((before_count, remaining) == expected, "%s cleanup rehearsal" % name)

    head("8. Sharding: stateless workers give the serial answer")
    shard_corpus = []
    for i in range(12):
        body = "import os\n\n\ndef f%d(x=[]):\n    if x == None:\n        ValueError('e%d')\n    return x   \n" % (i, i)
        shard_corpus.append(("m%02d.py" % i, body))
    serial = [n for job in shard_corpus for n in worker(job)]
    with ProcessPoolExecutor(max_workers=2) as pool:
        parallel = [n for batch in pool.map(worker, shard_corpus) for n in batch]
    print("files=%d notes=%d identical=%s" % (len(shard_corpus), len(serial), serial == parallel))
    check(serial == parallel and len(serial) == 12 * 5, "sharded run matches serial")

    head("9. Feedback policy on synthetic click counts")
    for label, nu, pf, af, expected in (("no-signal", 0, 0, 0, None), ("healthy", 3, 600, 300, 3 / 903),
                                       ("boundary-10pct", 10, 60, 30, 0.10),
                                       ("boundary-25pct", 25, 50, 25, 0.25), ("noisy", 26, 50, 24, 0.26)):
        rate = not_useful_rate(nu, pf, af)
        check(rate == expected, "%s feedback denominator" % label)
        print("%-15s NOT_USEFUL=%-3d PLEASE_FIX=%-3d APPLY_FIX=%-3d rate=%s -> %s" % (
            label, nu, pf, af, "n/a" if rate is None else "%.3f" % rate, disposition(rate)))
    check(disposition(None) == "no-signal" and disposition(0.10) == "probation"
          and disposition(0.25) == "probation" and disposition(0.251) == "disable-candidate",
          "threshold edges")

    head("10. The same fix as SARIF 2.1.0")
    dead = [(fp, n) for fp, n in fingerprints(new_notes) if n.category == "DeadException"]
    log = to_sarif(dead)
    check(log["runs"][0]["columnKind"] == "unicodeCodePoints", "SARIF declares the text coordinate unit")
    print(dumps(log["runs"][0]["results"][0]))
    off = log["runs"][0]["results"][0]["fixes"][0]["artifactChanges"][0]["replacements"][0]["deletedRegion"]["charOffset"]
    check(NEW[off:off + len("ValueError")] == "ValueError", "charOffset points at the call")


if __name__ == "__main__":
    main()
