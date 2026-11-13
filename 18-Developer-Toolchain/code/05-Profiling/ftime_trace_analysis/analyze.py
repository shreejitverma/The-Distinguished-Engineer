"""Read clang -ftime-trace JSON files and print facts that do not change between runs.

The analysis follows the algorithm of ClangBuildAnalyzer v1.6.0 (src/BuildEvents.cpp and
src/Analysis.cpp): merge each async "b" event with the "e" that follows it, rebuild the
event tree from time containment alone, sum header cost from "Source" spans, and collapse
template arguments to "$" to form template sets, skipping an instantiation whose ancestor
belongs to the same set.

Wall-clock durations differ on every run, so the script prints counts, names, and
comparisons that hold by a wide margin, and raises when a property the note relies on
does not hold.
Standard library only.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


class CheckFailed(Exception):
    pass


def check(cond: bool, what: str) -> None:
    if not cond:
        raise CheckFailed(what)


@dataclass
class Event:
    name: str
    ts: int
    dur: int
    detail: str
    args: dict
    index: int
    parent: int = -1
    children: list[int] = field(default_factory=list)


@dataclass
class Trace:
    path: Path
    raw: dict
    events: list[Event]  # complete events, "b"/"e" merged, no "Total" or metadata
    totals: dict[str, tuple[int, int]]  # name -> (count, dur_us)


def load(path: Path) -> Trace:
    raw = json.loads(path.read_text(encoding="utf-8"))
    events: list[Event] = []
    totals: dict[str, tuple[int, int]] = {}
    pending_b: dict | None = None
    for ev in raw["traceEvents"]:
        ph = ev["ph"]
        name = ev["name"]
        if ph == "M":
            continue
        if ph == "X" and name.startswith("Total "):
            totals[name[len("Total ") :]] = (ev["args"]["count"], ev["dur"])
            continue
        if ph == "b":
            check(pending_b is None, f"{path.name}: nested async begin")
            pending_b = ev
            continue
        if ph == "e":
            # LLVM's TimeProfiler writes each async end right after its begin.
            check(pending_b is not None and pending_b["name"] == name, f"{path.name}: unpaired async end")
            b = pending_b
            pending_b = None
            args = b.get("args", {})
            events.append(Event(name, b["ts"], ev["ts"] - b["ts"], args.get("detail", ""), args, len(events)))
            continue
        if ph == "X":
            args = ev.get("args", {})
            events.append(Event(name, ev["ts"], ev["dur"], args.get("detail", ""), args, len(events)))
    check(pending_b is None, f"{path.name}: async begin without end")
    build_tree(events)
    return Trace(path, raw, events, totals)


def build_tree(events: list[Event]) -> None:
    """Parent = innermost earlier event whose [ts, ts+dur] contains this one (ClangBuildAnalyzer's rule)."""
    order = sorted(range(len(events)), key=lambda i: (events[i].ts, -events[i].dur, -i))
    stack: list[int] = []
    for i in order:
        e = events[i]
        while stack:
            p = events[stack[-1]]
            if e.ts >= p.ts and e.ts + e.dur <= p.ts + p.dur:
                e.parent = p.index
                p.children.append(e.index)
                break
            stack.pop()
        stack.append(i)


def ancestors(t: Trace, e: Event):
    p = e.parent
    while p != -1:
        yield t.events[p]
        p = t.events[p].parent


def collapse(name: str) -> str:
    """Replace every outermost <...> argument list with <$>, as ClangBuildAnalyzer's CollapseName does."""
    if "operator" in name:
        return name
    out, depth = [], 0
    for ch in name:
        if ch == "<":
            if depth == 0:
                out.append("<$")
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                out.append(">")
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def basename(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def sources(t: Trace) -> list[Event]:
    return [e for e in t.events if e.name == "Source"]


def frontend_us(t: Trace) -> int:
    return sum(e.dur for e in t.events if e.name == "Frontend")


def backend_us(t: Trace) -> int:
    return sum(e.dur for e in t.events if e.name == "Backend")


def count_inst(t: Trace, kind: str, needle: str) -> int:
    return sum(1 for e in t.events if e.name == kind and needle in e.detail)


# ---------------------------------------------------------------- sections


def section_anatomy(t: Trace) -> None:
    print("== 2. Anatomy of one trace (tu_a.cpp, -O2, granularity 0)")
    print("top-level keys:", ", ".join(sorted(t.raw)))
    phases = Counter(ev["ph"] for ev in t.raw["traceEvents"])
    print("phases:", " ".join(f"{k}={phases[k]}" for k in sorted(phases)))
    total_tids = {ev["tid"] for ev in t.raw["traceEvents"] if ev["name"].startswith("Total ")}
    print(f"'Total <name>' summary events: {len(t.totals)}, each on its own tid: {'yes' if len(total_tids) == len(t.totals) else 'no'}")
    srcs = sources(t)
    print(f"Source spans written as async b/e pairs: {phases['b']} (speedscope 1.25.0 imports only B, E, X)")
    print("Source spans:", ", ".join(basename(e.detail) for e in sorted(srcs, key=lambda e: e.ts)))
    fronts = [e for e in t.events if e.name == "Frontend"]
    print(f"Frontend events: {len(fronts)} (ParseAST, then BackendConsumer::HandleTranslationUnit)")
    last_x = [ev for ev in t.raw["traceEvents"] if ev["ph"] == "X" and not ev["name"].startswith("Total ")][-1]
    print("last complete event written:", last_x["name"])
    roots = [e for e in t.events if e.parent == -1]
    print("tree roots after containment rebuild:", ", ".join(sorted({e.name for e in roots})))
    fam = Counter(collapse(e.detail) for e in t.events if e.name == "InstantiateClass")
    print("InstantiateClass events by template set:")
    for name, n in sorted(fam.items()):
        print(f"  {name}: {n}")
    heavy = [e for e in srcs if e.detail.endswith("heavy_meta.hpp")]
    check(len(heavy) == 1, "tu_a has one heavy_meta.hpp Source span")
    fib = [e for e in t.events if e.name == "InstantiateClass" and e.detail.startswith("meta::Fib<")]
    inside = all(heavy[0] in ancestors(t, e) for e in fib)
    print(f"every meta::Fib instantiation nests inside the heavy_meta.hpp Source span: {'yes' if inside else 'no'}")
    def depth(e: Event) -> int:
        return sum(1 for a in ancestors(t, e) if a.name == "InstantiateClass")

    nested_fib = sum(1 for e in fib if depth(e) > 0)
    print(f"meta::Fib events nested inside another InstantiateClass: {nested_fib} of {len(fib)}")
    ic = [e for e in t.events if e.name == "InstantiateClass"]
    deepest = max(ic, key=depth)
    print(f"deepest InstantiateClass nesting: {depth(deepest)} levels, under {collapse(deepest.detail)}")
    print()


def section_granularity(g0: Trace, g500: Trace) -> None:
    print("== 3. Granularity filters events, not totals")
    n0, n500 = len(g0.events), len(g500.events)
    print(f"complete and async events kept, granularity 0 us: {n0}; default 500 us: {'fewer' if n500 < n0 else 'NOT fewer'}")
    c0, d0 = g0.totals["InstantiateClass"]
    c500, _ = g500.totals["InstantiateClass"]
    print(f"Total InstantiateClass count, granularity 0: {c0}; granularity 500: {c500}; equal: {'yes' if c0 == c500 else 'no'}")
    ic = [e for e in g0.events if e.name == "InstantiateClass"]
    topmost = [e for e in ic if not any(a.name == "InstantiateClass" for a in ancestors(g0, e))]
    print(f"InstantiateClass events in the granularity-0 trace: {len(ic)}, topmost only: {len(topmost)}")
    print(f"Total count equals the topmost-only count: {'yes' if c0 == len(topmost) else 'no'}")
    top_sum = sum(e.dur for e in topmost)
    # Each event's dur is truncated to whole microseconds; the Total keeps full precision.
    close = abs(top_sum - d0) <= len(topmost) + 1
    print(f"Total InstantiateClass dur equals the sum over topmost events (within 1 us each): {'yes' if close else 'no'}")
    all_sum = sum(e.dur for e in ic)
    print(f"sum of every InstantiateClass dur is more than 10x the Total (nested time counted at every level): {'yes' if all_sum > 10 * d0 else 'no'}")
    print()


def section_build(traces: list[Trace]) -> None:
    print("== 4. Build-wide aggregation (ClangBuildAnalyzer's algorithm over 5 traces)")
    print("dominant phase per TU (Frontend vs Backend, needs a 3x margin):")
    for t in traces:
        if t.path.stem == "main":
            continue  # main.cpp is near-empty; neither phase wins by a stable margin
        f, b = frontend_us(t), backend_us(t)
        if f > 3 * b:
            verdict = "Frontend"
        elif b > 3 * f:
            verdict = "Backend"
        else:
            verdict = "no 3x winner"
        print(f"  {t.path.stem}: {verdict}")
    header_us: Counter[str] = Counter()
    header_n: Counter[str] = Counter()
    for t in traces:
        for e in sources(t):
            if e.detail.endswith((".h", ".hpp")):
                header_us[basename(e.detail)] += e.dur
                header_n[basename(e.detail)] += 1
    print("header inclusions:", ", ".join(f"{h} {n}x" for h, n in sorted(header_n.items())))
    ranked = header_us.most_common()
    top, second = ranked[0], ranked[1]  # second only sets the margin; its name is run-to-run noise
    margin = top[1] > 10 * second[1]
    print(f"most expensive header by summed Source time: {top[0]} (10x ahead of every other header: {'yes' if margin else 'no'})")
    fe_total = sum(frontend_us(t) for t in traces)
    print(f"{top[0]} share of all Frontend time over 50%: {'yes' if top[1] * 2 > fe_total else 'no'}")
    sets_n: Counter[str] = Counter()
    sets_us: Counter[str] = Counter()
    raw_n: Counter[str] = Counter()
    for t in traces:
        for e in t.events:
            if e.name not in ("InstantiateClass", "InstantiateFunction"):
                continue
            name = collapse(e.detail)
            raw_n[name] += 1
            recursive = any(
                a.name in ("InstantiateClass", "InstantiateFunction") and collapse(a.detail) == name
                for a in ancestors(t, e)
            )
            if not recursive:
                sets_n[name] += 1
                sets_us[name] += e.dur
    print("template sets: raw instantiation events -> counted after skipping same-set ancestors:")
    for name in sorted(raw_n):
        print(f"  {name}: {raw_n[name]} -> {sets_n[name]}")
    (s1, u1), (_, u2) = sets_us.most_common(2)
    print(f"most expensive template set: {s1} (10x ahead of every other set: {'yes' if u1 > 10 * u2 else 'no'})")
    print()


def widget_counts(traces: list[Trace]) -> tuple[int, int]:
    crunch = sum(count_inst(t, "InstantiateFunction", "Widget<int>::crunch") for t in traces)
    fast = sum(count_inst(t, "InstantiateFunction", "Widget<int>::fast") for t in traces)
    return crunch, fast


def section_extern(variants: list[tuple[str, list[Trace]]], inst: Trace) -> None:
    print("== 5. extern template: who instantiates Widget<int> members")
    print("InstantiateFunction events summed over tu_a, tu_b, tu_c:")
    print(f"  {'configuration':<22} {'crunch (out of class)':<23} fast (in class, inline)")
    for label, traces in variants:
        c, f = widget_counts(traces)
        print(f"  {label:<22} {c:<23} {f}")
    c, f = widget_counts([inst])
    print(f"widget_inst.cpp (explicit instantiation definition): crunch {c}, fast {f}")
    print()


def section_pch(plain: Trace, with_pch: Trace, pch_build: Trace) -> None:
    print("== 6. A precompiled header moves the header cost into one compile")

    def heavy_spans(t: Trace) -> int:
        return sum(1 for e in sources(t) if e.detail.endswith("heavy_meta.hpp"))

    def fib(t: Trace) -> int:
        return count_inst(t, "InstantiateClass", "meta::Fib<")

    print(f"heavy_meta.hpp Source spans: tu_a {heavy_spans(plain)}, tu_a with -include-pch {heavy_spans(with_pch)}")
    print(f"meta::Fib InstantiateClass events: tu_a {fib(plain)}, tu_a with -include-pch {fib(with_pch)}, PCH build {fib(pch_build)}")
    fe_plain, fe_pch = frontend_us(plain), frontend_us(with_pch)
    print(f"tu_a Frontend time is lower with the PCH: {'yes' if fe_pch < fe_plain else 'no'}")
    ast = ("ReadAST", "Read Loaded AST")
    seen = {e.name for e in with_pch.events}
    print("AST-loading events: without PCH [" + ", ".join(n for n in ast if n in {e.name for e in plain.events}) + "],"
          " with PCH [" + ", ".join(n for n in ast if n in seen) + "]")
    print()


def section_verbose(plain: Trace, verbose: Trace) -> None:
    print("== 7. -ftime-trace-verbose")

    def keys(t: Trace, kind: str) -> str:
        ks: set[str] = set()
        for e in t.events:
            if e.name == kind:
                ks |= set(e.args)
        return ", ".join(sorted(ks)) or "(none)"

    for kind in ("InstantiateClass", "InstantiateFunction", "Frontend"):
        print(f"{kind} args keys: default [{keys(plain, kind)}], verbose [{keys(verbose, kind)}]")
    fib = [e for e in verbose.events if e.name == "InstantiateClass" and e.detail == "meta::Fib<900>"]
    check(len(fib) == 1, "one meta::Fib<900> event")
    print(f"meta::Fib<900> location: {basename(fib[0].args['file'])}:{fib[0].args['line']}")
    print()


def main() -> int:
    w = Path(sys.argv[1])
    try:
        section_anatomy(load(w / "g0" / "tu_a.json"))
        section_granularity(load(w / "g0" / "tu_a.json"), load(w / "g500" / "tu_a.json"))
        base = [load(w / "base" / f"{s}.json") for s in ("backend_heavy", "main", "tu_a", "tu_b", "tu_c")]
        section_build(base)
        tus = ("tu_a", "tu_b", "tu_c")
        variants = [
            (label, [load(w / d / f"{s}.json") for s in tus])
            for label, d in (
                ("implicit, -O0", "imp_O0"),
                ("implicit, -O2", "imp_O2"),
                ("extern template, -O0", "ext_O0"),
                ("extern template, -O2", "ext_O2"),
            )
        ]
        section_extern(variants, load(w / "ext_O2" / "widget_inst.json"))
        section_pch(load(w / "g0" / "tu_a.json"), load(w / "pch" / "tu_a.json"), load(w / "pch" / "heavy_meta.json"))
        section_verbose(load(w / "g0" / "tu_a.json"), load(w / "verbose" / "tu_a.json"))
    except CheckFailed as exc:
        print(f"CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
