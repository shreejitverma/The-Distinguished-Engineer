"""sys.monitoring lab: every section prints facts that change if the mechanism is absent.

Run through run.sh. Each numbered section runs in its own interpreter process, so no
section depends on the tool ids, DISABLEd locations, or specialized bytecode that another
one left behind. Output holds no addresses, paths, or timings: only counts, opcode names,
byte sizes, relative line numbers, and threshold verdicts.
"""

from __future__ import annotations

import bdb
import collections
import cProfile
import dis
import gc
import os
import subprocess
import sys
import threading
import time
import tracemalloc

import minitrace
import target

MON = sys.monitoring
EV = MON.events
VER = sys.version_info[:2]


def section(title: str) -> None:
    print()
    print(f"== {title}")


def err(fn, *args) -> str:
    try:
        fn(*args)
    except ValueError as e:
        return f"ValueError: {e}"
    return "no error"


def instrumented(code) -> list[str]:
    return [i.opname for i in dis.get_instructions(code, adaptive=True) if i.opname.startswith("INSTRUMENTED_")]


def summary(ops: list[str]) -> str:
    if not ops:
        return "none"
    return ", ".join(f"{name} x{n}" for name, n in sorted(collections.Counter(ops).items()))


def tool_ids() -> None:
    section("1. Tool identifiers")
    print(
        f"named ids: DEBUGGER_ID={MON.DEBUGGER_ID} COVERAGE_ID={MON.COVERAGE_ID} "
        f"PROFILER_ID={MON.PROFILER_ID} OPTIMIZER_ID={MON.OPTIMIZER_ID}"
    )
    print("use_tool_id(6):", err(MON.use_tool_id, 6, "x"))
    MON.use_tool_id(0, "lab-debugger")
    print("get_tool(0) after claim:", repr(MON.get_tool(0)))
    print("second use_tool_id(0):", err(MON.use_tool_id, 0, "other"))
    MON.free_tool_id(0)
    print("get_tool(0) after free:", repr(MON.get_tool(0)))
    print("set_events(3) on unclaimed id:", err(MON.set_events, 3, EV.LINE))
    print("get_tool(7), the hidden settrace slot:", err(MON.get_tool, 7))

    def profile_briefly() -> None:
        prof = cProfile.Profile()
        prof.enable()
        prof.disable()

    print("cProfile with PROFILER_ID free:", err(profile_briefly))
    MON.use_tool_id(MON.PROFILER_ID, "lab-profiler")
    print("cProfile while another tool holds PROFILER_ID:", err(profile_briefly))
    MON.free_tool_id(MON.PROFILER_ID)

    MON.use_tool_id(MON.DEBUGGER_ID, "pydevd")
    # Bdb.start_trace() is the step of set_trace() that claims the id; calling it
    # alone avoids set_trace() installing f_trace on every caller frame first.
    print(
        "bdb monitoring backend while DEBUGGER_ID is held by 'pydevd':", err(bdb.Bdb(backend="monitoring").start_trace)
    )
    MON.free_tool_id(MON.DEBUGGER_ID)


def event_bits() -> None:
    section("2. Events are bits (bit numbers differ between releases)")
    names = sorted((n for n in dir(EV) if n.isupper() and n != "NO_EVENTS"), key=lambda n: getattr(EV, n))
    print(" ".join(f"{n}={getattr(EV, n).bit_length() - 1}" for n in names))
    MON.use_tool_id(2, "lab-bits")
    print("set_events(CALL | C_RETURN):", err(MON.set_events, 2, EV.CALL | EV.C_RETURN))
    MON.set_events(2, EV.CALL | EV.C_RETURN | EV.C_RAISE)
    print("get_events after CALL|C_RETURN|C_RAISE == CALL:", MON.get_events(2) == EV.CALL)
    MON.set_events(2, EV.BRANCH)
    print(
        "deprecated BRANCH stored as BRANCH_LEFT|BRANCH_RIGHT:", MON.get_events(2) == EV.BRANCH_LEFT | EV.BRANCH_RIGHT
    )
    MON.free_tool_id(2)


def bytecode_rewrite() -> None:
    section("3. Turning events on rewrites the code object's bytecode")
    code = target.classify.__code__
    raw = code.co_code
    print("before:", summary(instrumented(code)))
    MON.use_tool_id(1, "lab-rewrite")
    MON.register_callback(1, EV.PY_START, lambda c, o: None)
    MON.register_callback(1, EV.LINE, lambda c, ln: MON.DISABLE)
    MON.set_local_events(1, code, EV.LINE | EV.PY_START)
    print("after set_local_events(LINE | PY_START):", summary(instrumented(code)))
    print("co_code still the original bytes:", code.co_code == raw)
    target.classify(5)
    print("after classify(5), every LINE callback returned DISABLE:", summary(instrumented(code)))
    MON.set_local_events(1, code, 0)
    print("after set_local_events(0):", summary(instrumented(code)))
    MON.free_tool_id(1)
    MON.restart_events()


def disable_fast_path() -> None:
    section("4. DISABLE: one callback per location, then none")
    code = target.hot_loop.__code__

    def run(tool_id: int, disable: bool, calls: int = 1) -> tuple[int, int]:
        seen: list[int] = []

        def cb(c, line):
            if c is code:
                seen.append(line)
            return MON.DISABLE if disable else None

        MON.use_tool_id(tool_id, "lab-disable")
        MON.register_callback(tool_id, EV.LINE, cb)
        MON.set_events(tool_id, EV.LINE)
        counts = []
        for _ in range(calls):
            before = len(seen)
            target.hot_loop(1000)
            counts.append(len(seen) - before)
        MON.free_tool_id(tool_id)
        return counts, len(set(seen))

    counts, _ = run(1, disable=False)
    print("hot_loop(1000), callback returns None: LINE callbacks =", counts[0])
    counts, lines = run(2, disable=True, calls=2)
    print(
        f"callback returns DISABLE: first call = {counts[0]}, second call = {counts[1]}, distinct line numbers = {lines}"
    )
    counts, _ = run(2, disable=False)
    print("id 2 freed and claimed again, hot_loop not run in between:", counts[0])
    MON.restart_events()
    counts, _ = run(2, disable=False)
    print("id 2 after restart_events():", counts[0])
    run(2, disable=True)
    target.hot_loop(10)  # runs while id 2 is free, so RESUME re-instruments it
    counts, _ = run(2, disable=False)
    print("id 2 DISABLEd, freed, hot_loop run while free, claimed again:", counts[0])


def local_vs_global() -> None:
    section("5. Local events versus global events")
    per = collections.Counter()

    def cb(c, line):
        if c.co_filename == target.__file__:
            per[c.co_name] += 1

    MON.use_tool_id(1, "lab-local")
    MON.register_callback(1, EV.LINE, cb)
    MON.set_local_events(1, target.classify.__code__, EV.LINE)
    target.classify(1)
    target.fib(5)
    print("local LINE on classify only:", dict(sorted(per.items())))
    per.clear()
    MON.set_events(1, EV.LINE)
    target.classify(1)
    target.fib(5)
    both = dict(sorted(per.items()))
    MON.set_local_events(1, target.classify.__code__, 0)
    per.clear()
    target.classify(1)
    target.fib(5)
    only_global = dict(sorted(per.items()))
    print("global LINE plus local LINE on classify:", both)
    print("global LINE only:", only_global)
    print("an event on both lists fires once:", both == only_global)
    MON.free_tool_id(1)


def many_tools() -> None:
    section("6. Several tools on one event")
    order: list[int] = []
    counts = collections.Counter()

    def make(tool_id: int):
        def cb(c, off):
            if c is target.fib.__code__:
                order.append(tool_id)
                counts[tool_id] += 1
                if tool_id == 1:
                    return MON.DISABLE
            return None

        return cb

    for t in (0, 1, 2):
        MON.use_tool_id(t, f"lab-tool-{t}")
        MON.register_callback(t, EV.PY_START, make(t))
        MON.set_events(t, EV.PY_START)
    target.fib(10)
    print("callback order for the first fib() call:", order[:3])
    print("PY_START callbacks over fib(10) per tool id:", dict(sorted(counts.items())))
    print("tool 1 returned DISABLE; tools 0 and 2 still see every call:", counts[0] == counts[2] == 177)
    for t in (0, 1, 2):
        MON.free_tool_id(t)
    MON.restart_events()


def no_reentry() -> None:
    section("7. Events raised inside a callback are not reported")
    starts = collections.Counter()

    def cb(c, off):
        starts[c.co_name] += 1
        if c.co_name == "tiny":
            target.classify(1)

    MON.use_tool_id(2, "lab-reentry")
    MON.register_callback(2, EV.PY_START, cb)
    MON.set_events(2, EV.PY_START)
    target.tiny()
    MON.set_events(2, 0)
    print("PY_START for tiny:", starts["tiny"], "| for classify called from the callback:", starts["classify"])
    MON.free_tool_id(2)


def threads() -> None:
    section("8. Scope: sys.monitoring is per interpreter, sys.settrace is per thread")
    seen = {"monitoring": set(), "settrace": set()}

    def mon_cb(c, off):
        if c is target.tiny.__code__:
            seen["monitoring"].add(threading.current_thread().name)

    def tracer(frame, event, arg):
        if frame.f_code is target.tiny.__code__:
            seen["settrace"].add(threading.current_thread().name)

    MON.use_tool_id(0, "lab-threads")
    MON.register_callback(0, EV.PY_START, mon_cb)
    MON.set_events(0, EV.PY_START)
    sys.settrace(tracer)
    target.tiny()
    worker = threading.Thread(target=target.tiny, name="worker")
    worker.start()
    worker.join()
    sys.settrace(None)
    MON.free_tool_id(0)
    for k, v in seen.items():
        print(f"{k} saw tiny() in:", sorted(v))


def error_rules() -> None:
    section("9. What DISABLE cannot do, and failing callbacks")
    MON.use_tool_id(2, "lab-errors")
    MON.register_callback(2, EV.CALL, lambda *a: None)
    MON.register_callback(2, EV.C_RETURN, lambda *a: MON.DISABLE)
    stage = "set_events() itself returning"
    try:
        MON.set_events(2, EV.CALL | EV.C_RETURN | EV.C_RAISE)
        stage = "the next C call, len(), returning"
        len("abc")
        print("C_RETURN DISABLE: no error")
    except ValueError as e:
        print(f"C_RETURN DISABLE: {e} (raised at {stage})")
    print("C_RETURN callback still registered:", MON.register_callback(2, EV.C_RETURN, None) is not None)
    MON.free_tool_id(2)

    def boom(c, off):
        if c is target.tiny.__code__:
            raise RuntimeError("callback failed")

    MON.use_tool_id(2, "lab-raises")
    MON.register_callback(2, EV.PY_START, boom)
    MON.set_events(2, EV.PY_START)
    outcomes = []
    for _ in range(2):
        try:
            target.tiny()
            outcomes.append("returned")
        except RuntimeError as e:
            outcomes.append(f"RuntimeError({e})")
    still = MON.get_events(2) == EV.PY_START
    MON.free_tool_id(2)
    print("callback that raises, two calls of tiny():", outcomes, "| events still on:", still)

    def bad_tracer(frame, event, arg):
        if frame.f_code is target.tiny.__code__:
            raise RuntimeError("tracer failed")

    outcomes = []
    sys.settrace(bad_tracer)
    for _ in range(2):
        try:
            target.tiny()
            outcomes.append("returned")
        except RuntimeError as e:
            outcomes.append(f"RuntimeError({e})")
    removed = sys.gettrace() is None
    sys.settrace(None)
    print("settrace function that raises, two calls:", outcomes, "| tracer removed itself:", removed)

    code = target.risky.__code__
    for mode in ("global", "local"):
        hits = [0]

        def on_raise(c, off, exc, hits=hits):
            if c is code:
                hits[0] += 1
                return MON.DISABLE
            return None

        MON.use_tool_id(2, "lab-raise")
        MON.register_callback(2, EV.RAISE, on_raise)
        try:
            if mode == "global":
                MON.set_events(2, EV.RAISE)
            else:
                MON.set_local_events(2, code, EV.RAISE)
            target.risky(5)
            print(f"RAISE enabled {mode}ly, callback returns DISABLE: {hits[0]} of 5 raises reported")
        except ValueError as e:
            print(f"RAISE enabled {mode}ly, callback returns DISABLE: ValueError: {e} ({hits[0]} reported)")
        MON.free_tool_id(2)
        MON.restart_events()


def legacy() -> None:
    section("10. sys.settrace runs as a hidden tool next to sys.monitoring")
    code = target.tiny.__code__
    seq: list[str] = []

    def tracer(frame, event, arg):
        if frame.f_code is code:
            seq.append(f"settrace:{event}")
            return tracer
        return None

    def profiler(frame, event, arg):
        if frame.f_code is code:
            seq.append(f"setprofile:{event}")

    MON.use_tool_id(0, "lab-legacy")
    MON.register_callback(0, EV.LINE, lambda c, ln: seq.append("monitoring:LINE") if c is code else None)
    MON.register_callback(0, EV.PY_START, lambda c, off: seq.append("monitoring:PY_START") if c is code else None)
    MON.set_events(0, EV.LINE | EV.PY_START)
    sys.settrace(tracer)
    sys.setprofile(profiler)
    target.tiny()
    sys.setprofile(None)
    sys.settrace(None)
    MON.free_tool_id(0)
    print("event order inside tiny():")
    print("  " + ", ".join(seq))


def consume(n: int) -> int:
    total = 0
    for v in target.countdown(n):
        total += v
    return total


def generators() -> None:
    section("11. Generator events, and how the loop exit is reported")
    log: list[str] = []
    who = {target.countdown.__code__: "gen", consume.__code__: "caller"}

    def make(name: str):
        def cb(c, off, *rest):
            if c in who:
                log.append(f"{who[c]}:{name}")

        return cb

    base = ["PY_START", "PY_RESUME", "PY_YIELD", "PY_RETURN", "STOP_ITERATION", "RAISE"]
    for label, names in (
        ("without branch events", base),
        ("with BRANCH_LEFT and BRANCH_RIGHT", base + ["BRANCH_LEFT", "BRANCH_RIGHT"]),
    ):
        log.clear()
        MON.use_tool_id(2, "lab-gen")
        events = 0
        for name in names:
            MON.register_callback(2, getattr(EV, name), make(name))
            events |= getattr(EV, name)
        MON.set_events(2, events)
        consume(2)
        MON.free_tool_id(2)
        MON.restart_events()
        print(f"consume(2), {label}:")
        print("  " + " ".join(log))


def rebuild() -> None:
    section("12. Rebuild: four tools in minitrace.py")
    code = target.classify.__code__
    with minitrace.LineCoverage([code]) as cov:
        target.classify(-1)
        target.classify(0)
    ref = set()
    for arg in (-1, 0):
        ref |= minitrace.settrace_line_coverage(target.classify, arg)
    print("line coverage of classify(-1), classify(0), lines relative to def:", cov.relative(code))
    print("never executed:", sorted({1, 2, 3, 4, 5} - set(cov.relative(code))))
    print("LINE callbacks during those two calls (each location is reported once, then DISABLEd):", cov.callbacks)
    print("matches a sys.settrace reference tracer:", set(cov.relative(code)) == ref)

    with minitrace.BranchCoverage(code) as br:
        target.classify(-1)
        target.classify(0)
    print("branch arcs (side, from line, to line) after -1 and 0:", sorted(br.arcs))
    with minitrace.BranchCoverage(code) as br:
        for arg in (-1, 0, 7):
            target.classify(arg)
    print("branch arcs after -1, 0, 7:", len(br.arcs), "of 4")

    with minitrace.CallCounter() as prof:
        target.fib(10)
    print("fib(10): calls =", prof.calls["fib"], "| max depth =", prof.max_depth)
    print(
        "  a naive depth counter (+1 on start, -1 on return) on the same events: max =",
        prof.naive_max,
        "| lowest =",
        prof.naive_min,
    )
    print("  returns with no start (frames running when events went on):", prof.unmatched_returns)
    print("  starts with no return (frames running when events went off):", [c.co_qualname for c in prof.stack])

    with minitrace.Breakpoints(target.hot_loop.__code__, [4], "i") as bp:
        target.hot_loop(10)
        target.hot_loop(10)
    print(
        "breakpoint on hot_loop line +4 over two hot_loop(10) calls: hits =",
        len(bp.hits),
        "| i at hits =",
        [v for _, v in bp.hits],
    )
    print("LINE callbacks in total:", bp.callbacks, "(hits plus one per other location)")


def exception_events() -> None:
    section("13. Exception events across frames, and what sys.settrace makes of them")
    who = {
        target.inner.__code__: "inner",
        target.middle.__code__: "middle",
        target.outer.__code__: "outer",
        target.echo.__code__: "echo",
    }
    log: list[str] = []

    def make(name: str):
        def cb(c, off, *rest):
            if c in who:
                log.append(f"{who[c]}:{name}")

        return cb

    names = ["RAISE", "RERAISE", "EXCEPTION_HANDLED", "PY_UNWIND", "PY_THROW", "PY_RETURN", "PY_YIELD"]
    MON.use_tool_id(3, "lab-exc")
    events = 0
    for name in names:
        MON.register_callback(3, getattr(EV, name), make(name))
        events |= getattr(EV, name)
    MON.set_events(3, events)
    result = target.outer(1)
    one_exception = list(log)
    log.clear()
    target.throw_into_echo()
    thrown = list(log)
    MON.free_tool_id(3)
    print(
        f"outer(1) returned {result!r}; one KeyError raised in inner, passed through middle's finally, caught in outer:"
    )
    print("  " + " ".join(one_exception))
    counts = collections.Counter(entry.split(":")[1] for entry in one_exception)
    print(
        "events for that one exception:",
        ", ".join(f"{name} x{counts[name]}" for name in ("RAISE", "EXCEPTION_HANDLED", "RERAISE", "PY_UNWIND")),
    )
    print("gen.throw(ValueError) into a generator that catches it and yields again:")
    print("  " + " ".join(thrown[: thrown.index("echo:PY_YIELD", 1) + 1]))

    legacy: list[str] = []

    def tracer(frame, event, arg):
        if frame.f_code in who:
            if event != "line":
                shown = "None" if arg is None else type(arg).__name__
                legacy.append(f"{who[frame.f_code]}:{event}({shown})")
            return tracer
        return None

    sys.settrace(tracer)
    target.outer(1)
    sys.settrace(None)
    print("sys.settrace on the same outer(1) call, line events left out, argument type in brackets:")
    print("  " + " ".join(legacy))


def call_events() -> None:
    section("14. CALL and its ancillary events, and what sys.setprofile makes of them")
    code = target.call_kinds.__code__
    log: list[str] = []

    def shown(arg0) -> str:
        return "MISSING" if arg0 is MON.MISSING else type(arg0).__name__

    def make(name: str):
        def cb(c, off, callable_, arg0):
            if c is code:
                log.append(f"{name}({callable_.__qualname__}, arg0={shown(arg0)})")

        return cb

    MON.use_tool_id(3, "lab-calls")
    for name in ("CALL", "C_RETURN", "C_RAISE"):
        MON.register_callback(3, getattr(EV, name), make(name))
    MON.set_local_events(3, code, EV.CALL | EV.C_RETURN | EV.C_RAISE)
    print("get_local_events after CALL|C_RETURN|C_RAISE == CALL:", MON.get_local_events(3, code) == EV.CALL)
    target.call_kinds()
    MON.free_tool_id(3)
    print("call_kinds(), local CALL | C_RETURN | C_RAISE:")
    for entry in log:
        print("  " + entry)

    legacy: list[str] = []

    def profiler(frame, event, arg):
        if event.startswith("c_"):
            if frame.f_code is code:
                legacy.append(f"{event}({arg.__qualname__})")
        elif frame.f_back is not None and frame.f_back.f_code is code:
            legacy.append(f"{event}({frame.f_code.co_qualname})")

    sys.setprofile(profiler)
    target.call_kinds()
    sys.setprofile(None)
    print("sys.setprofile on the same call_kinds():")
    print("  " + " ".join(legacy))


def legacy_is_monitoring() -> None:
    section("15. Legacy hooks are monitoring tools, and how to find out who is monitoring")
    code = target.classify.__code__
    hits = [0]

    def on_instruction(c, off):
        if c is code:
            hits[0] += 1

    MON.use_tool_id(3, "lab-instr")
    MON.register_callback(3, EV.INSTRUCTION, on_instruction)
    MON.set_local_events(3, code, EV.INSTRUCTION)
    target.classify(5)
    MON.free_tool_id(3)
    MON.restart_events()
    target.classify(5)

    opcodes = [0]

    def tracer(frame, event, arg):
        if frame.f_code is code:
            frame.f_trace_opcodes = True
            if event == "opcode":
                opcodes[0] += 1
            return tracer
        return None

    sys.settrace(tracer)
    target.classify(5)
    during = minitrace.instrumented_opcodes(code)
    sys.settrace(None)
    stale = minitrace.instrumented_opcodes(code)
    target.classify(5)
    print("INSTRUCTION events for classify(5), tool 3:", hits[0])
    print("sys.settrace 'opcode' events for classify(5), f_trace_opcodes = True:", opcodes[0])
    print("classify under sys.settrace with f_trace_opcodes:", during)
    print("right after sys.settrace(None), classify not run yet:", stale)
    print("after one more classify(5):", minitrace.instrumented_opcodes(code))

    def decline(frame, event, arg):
        return None

    loop = target.hot_loop.__code__
    sys.settrace(decline)
    target.hot_loop(0)
    cold = minitrace.instrumented_opcodes(loop)
    target.hot_loop(3)
    warm = minitrace.instrumented_opcodes(loop)
    sys.settrace(None)
    target.hot_loop(0)  # RESUME drops the legacy instrumentation again
    print("hot_loop under sys.settrace, loop body not run yet:", cold)
    print("after hot_loop(3), the legacy JUMP handler has DISABLEd the backward jumps:", warm)

    with minitrace.Breakpoints(loop, [4], "i"):
        MON.use_tool_id(3, "lab-global")
        MON.set_events(3, EV.PY_START | EV.RAISE)
        sys.settrace(decline)
        target.hot_loop(3)
        report = minitrace.who_is_monitoring(loop)
        hidden = err(MON.get_local_events, 7, loop)
        sys.settrace(None)
        MON.free_tool_id(3)
    print("who_is_monitoring(hot_loop) with a breakpoint tool, a global tool, and a settrace function that declines:")
    for line in report:
        print("  " + line)
    print("  get_local_events(7, hot_loop), asking for settrace's own events:", hidden)
    sys.setprofile(decline)
    target.tiny()
    under_profile = minitrace.instrumented_opcodes(target.tiny.__code__)
    sys.setprofile(None)
    print("tiny under sys.setprofile alone (no LINE):", under_profile)

    audit: collections.Counter[str] = collections.Counter()

    def hook(event, args):
        if event.startswith(("sys.monitoring", "sys.settrace", "sys.setprofile")):
            audit[event] += 1

    def audited(step) -> str:
        audit.clear()
        step()
        return ", ".join(f"{name} x{n}" for name, n in sorted(audit.items())) or "none"

    def claim_and_enable() -> None:
        MON.use_tool_id(3, "lab-audit")
        MON.set_events(3, EV.PY_START)
        MON.set_local_events(3, loop, EV.LINE)

    def trace_on_and_off() -> None:
        sys.settrace(decline)
        sys.settrace(None)

    sys.addaudithook(hook)  # audit hooks cannot be removed; this section's process ends right after
    print("audit events raised by use_tool_id + set_events + set_local_events:", audited(claim_and_enable))
    print("by register_callback(fn):", audited(lambda: MON.register_callback(3, EV.PY_START, on_instruction)))
    print("by free_tool_id:", audited(lambda: MON.free_tool_id(3)))
    print("by sys.settrace(fn) then sys.settrace(None):", audited(trace_on_and_off))


def specialization() -> None:
    section("16. An instrumented instruction is not specialized")
    code = target.call_loop.__code__

    def loop_ops() -> str:
        keep = ("FOR_ITER", "CALL", "INSTRUMENTED_CALL", "BINARY_OP")
        ins = [i for i in dis.get_instructions(code, adaptive=True) if i.opname.startswith(keep)]
        # The first CALL is range(n), which runs once per call; the lab watches the loop itself.
        return ", ".join(i.opname for i in ins[1:])

    target.call_loop(2000)
    print("for header, call to tiny(), += after 2000 warm iterations:", loop_ops())
    MON.use_tool_id(3, "lab-spec")
    MON.register_callback(3, EV.CALL, lambda c, off, fn, arg0: None)
    MON.set_local_events(3, code, EV.CALL)
    print("right after set_local_events(CALL):", loop_ops())
    target.call_loop(2000)
    print("after 2000 more iterations, callback returns None:", loop_ops())
    MON.register_callback(3, EV.CALL, lambda c, off, fn, arg0: MON.DISABLE)
    target.call_loop(1)
    print("callback returns DISABLE, after one iteration:", loop_ops())
    target.call_loop(2000)
    print("after 2000 more iterations:", loop_ops())
    MON.free_tool_id(3)
    MON.restart_events()


def _enable(tools: tuple[int, ...], code, events: int) -> None:
    for tool_id in tools:
        MON.set_local_events(tool_id, code, events)


def _never_monitored() -> int:
    return 1


def memory() -> None:
    section("17. Memory held by instrumentation side tables")
    base = target.hot_loop.__code__
    filename = _enable.__code__.co_filename
    call_line = _enable.__code__.co_firstlineno + 2  # the set_local_events() call

    def held(snapshot, line: int) -> int:
        """Bytes still allocated that were requested by the given line of this file."""
        total = 0
        for stat in snapshot.statistics("lineno"):
            frame = stat.traceback[0]
            if frame.filename == filename and frame.lineno == line:
                total += stat.size
        return total

    def measure(events: int, tools: tuple[int, ...]) -> tuple[int, int]:
        code = base.replace()  # a fresh code object with no instrumentation data yet
        for tool_id in tools:
            MON.use_tool_id(tool_id, "lab-mem")
        tracemalloc.start()
        _enable(tools, code, events)
        on = held(tracemalloc.take_snapshot(), call_line)
        for tool_id in tools:
            MON.set_local_events(tool_id, code, 0)
        off = held(tracemalloc.take_snapshot(), call_line)
        tracemalloc.stop()
        for tool_id in tools:
            MON.free_tool_id(tool_id)
        return on, off

    # First, while this process has never changed an event: a code object that no tool ever
    # asks about still gets the fixed-size record the first time it runs after the global
    # version moved.
    def_line = _never_monitored.__code__.co_firstlineno
    tracemalloc.start()
    _never_monitored()
    before = held(tracemalloc.take_snapshot(), def_line)
    MON.use_tool_id(3, "lab-mem")
    MON.set_events(3, EV.PY_START)
    MON.free_tool_id(3)
    _never_monitored()
    after = held(tracemalloc.take_snapshot(), def_line)
    tracemalloc.stop()
    print(
        f"a function that only runs while no event is on: {before} bytes before any event change, "
        f"{after} bytes after another tool's set_events() and free_tool_id()"
    )

    units = len(base.co_code) // 2
    print(
        f"hot_loop: {units} code units, {len(base.co_code)} bytes of bytecode; "
        "local events for tool 3, then for tools 3 and 4"
    )
    for label, events in (
        ("PY_START", EV.PY_START),
        ("LINE", EV.LINE),
        ("INSTRUCTION", EV.INSTRUCTION),
        ("LINE | INSTRUCTION", EV.LINE | EV.INSTRUCTION),
    ):
        one, one_off = measure(events, (3,))
        two, two_off = measure(events, (3, 4))
        print(
            f"{label}: one tool = {one} bytes, two tools = {two} bytes | "
            f"still held after set_local_events(0): {one_off}, {two_off}"
        )


def overhead() -> None:
    section("18. Overhead: counted work, then timing verdicts")
    code = target.hot_loop.__code__
    n = 20_000
    count = [0]

    def line_then_disable(c, line):
        if c is code:
            count[0] += 1
        return MON.DISABLE

    def line_counted(c, line):
        if c is code:
            count[0] += 1

    def local_tracer(frame, event, arg):
        count[0] += 1
        return local_tracer

    def line_tracer(frame, event, arg):
        count[0] += 1
        return local_tracer

    def decline(frame, event, arg):
        count[0] += 1  # falling off the end returns None, which declines to trace the frame

    def monitoring(callback):
        def setup() -> None:
            MON.use_tool_id(1, "lab-overhead")
            MON.register_callback(1, EV.LINE, callback)
            MON.set_events(1, EV.LINE)

        def teardown() -> None:
            MON.free_tool_id(1)
            MON.restart_events()

        return setup, teardown

    def settrace(tracer):
        return (lambda: sys.settrace(tracer)), (lambda: sys.settrace(None))

    modes = {
        "untraced": ((lambda: None), (lambda: None)),
        "LINE + DISABLE": monitoring(line_then_disable),
        "LINE callback without DISABLE": monitoring(line_counted),
        "sys.settrace line tracer": settrace(line_tracer),
        "sys.settrace that declines every frame": settrace(decline),
    }

    def line_opcodes() -> int:
        return sum(i.opname == "INSTRUMENTED_LINE" for i in dis.get_instructions(code, adaptive=True))

    print(f"Python-level callbacks during one hot_loop({n}) | INSTRUMENTED_LINE opcodes in hot_loop right after it")
    for name, (setup, teardown) in modes.items():
        runs = 2 if name == "LINE + DISABLE" else 1
        setup()
        for run in range(runs):
            count[0] = 0
            target.hot_loop(n)
            seen, left = count[0], line_opcodes()
            label = name if runs == 1 else f"{name}, run {run + 1}"
            print(f"  {label}: {seen} | {left}")
        teardown()
        target.hot_loop(1)  # lets RESUME drop instrumentation the mode left behind

    # Timing. Interference only ever adds time, so the minimum over many short, interleaved
    # samples estimates the undisturbed cost of each mode; a busy host makes single samples
    # useless, not the minimum. Sampling stops once every ratio is clear of its threshold,
    # and continues (up to a hard cap) while any ratio sits inside the guard band.
    thresholds = {
        "LINE + DISABLE": ("within", 1.5),
        "LINE callback without DISABLE": ("above", 2.5),
        "sys.settrace line tracer": ("above", 2.5),
        "sys.settrace that declines every frame": ("above", 1.2),
    }
    best = dict.fromkeys(modes, float("inf"))
    guard, first_rounds, more_rounds, max_rounds = 0.15, 30, 10, 400

    def sample_round() -> None:
        for name, (setup, teardown) in modes.items():
            setup()
            target.hot_loop(300)  # re-specialize, and spend the DISABLE callbacks, before timing
            t0 = time.perf_counter_ns()
            target.hot_loop(n)
            elapsed = time.perf_counter_ns() - t0
            teardown()
            best[name] = min(best[name], elapsed)

    def ratios() -> dict[str, float]:
        return {name: best[name] / best["untraced"] for name in thresholds}

    def settled() -> bool:
        return all(abs(ratio / thresholds[name][1] - 1.0) > guard for name, ratio in ratios().items())

    gc.disable()
    rounds = 0
    try:
        while rounds < first_rounds or (rounds < max_rounds and not settled()):
            for _ in range(first_rounds if rounds == 0 else more_rounds):
                sample_round()
                rounds += 1
    finally:
        gc.enable()

    final = ratios()
    print("timing verdicts (minimum over interleaved samples, as a ratio to the untraced minimum):")
    for name, (kind, limit) in thresholds.items():
        ok = final[name] < limit if kind == "within" else final[name] > limit
        if kind == "within":
            print(f"  {name} coverage within {limit}x of untraced:", "yes" if ok else "no")
        else:
            print(f"  {name} above {limit}x untraced:", "yes" if ok else "no")
    # Ratios vary by machine and load, so they go to stderr and stay out of the expected file.
    shown = ", ".join(f"{name} {ratio:.2f}x" for name, ratio in final.items())
    print(f"section 18 ratios after {rounds} rounds: {shown}", file=sys.stderr)


SECTIONS = (
    tool_ids,
    event_bits,
    bytecode_rewrite,
    disable_fast_path,
    local_vs_global,
    many_tools,
    no_reentry,
    threads,
    error_rules,
    legacy,
    generators,
    rebuild,
    exception_events,
    call_events,
    legacy_is_monitoring,
    specialization,
    memory,
    overhead,
)


def leftovers() -> list[str]:
    """Hooks a section failed to release; any entry is a lab bug."""
    left = [f"tool {i} {MON.get_tool(i)!r}" for i in range(6) if MON.get_tool(i) is not None]
    if sys.gettrace() is not None:
        left.append("sys.settrace")
    if sys.getprofile() is not None:
        left.append("sys.setprofile")
    return left


def run_section(number: int) -> int:
    SECTIONS[number - 1]()
    left = leftovers()
    if left:
        print(f"section {number} left hooks behind: {', '.join(left)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--section"]:
        return run_section(int(argv[1]))
    numbers = [int(a) for a in argv] or list(range(1, len(SECTIONS) + 1))
    print(f"python {VER[0]}.{VER[1]} ({sys.implementation.name})")
    for number in numbers:
        sys.stdout.flush()
        child = [sys.executable, "-E", "-s", "-B", os.path.abspath(__file__), "--section", str(number)]
        code = subprocess.run(child, check=False).returncode
        if code != 0:
            print(f"section {number} failed with exit status {code}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
