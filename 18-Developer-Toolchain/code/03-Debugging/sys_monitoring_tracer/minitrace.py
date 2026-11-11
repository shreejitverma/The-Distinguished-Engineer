"""Minimal tools built on PEP 669 sys.monitoring (CPython 3.14 or newer).

Each tool claims one tool identifier, registers callbacks, turns events on,
and releases everything in stop(). The tools are deliberately small: they show
the shape of a coverage tool, a branch coverage tool, a call counter, and a
breakpoint engine, not production replacements for coverage.py or pdb.
"""

from __future__ import annotations

import collections
import dis
import sys
from types import CodeType

MON = sys.monitoring
EV = MON.events


class Tool:
    """Owns one tool id for its lifetime."""

    def __init__(self, tool_id: int, name: str) -> None:
        self.tool_id = tool_id
        self.name = name

    def start(self) -> None:
        MON.use_tool_id(self.tool_id, self.name)

    def stop(self) -> None:
        # free_tool_id() clears events and callbacks (3.14+), but locations a
        # callback disabled with DISABLE stay disabled for this id until
        # restart_events(), so restart before handing the id back.
        MON.free_tool_id(self.tool_id)
        MON.restart_events()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


class LineCoverage(Tool):
    """Statement coverage: record each line once, then DISABLE that location."""

    def __init__(self, codes: list[CodeType]) -> None:
        super().__init__(MON.COVERAGE_ID, "minitrace-cov")
        self.codes = set(codes)
        self.hits: dict[CodeType, set[int]] = collections.defaultdict(set)
        self.callbacks = 0

    def _line(self, code: CodeType, line: int):
        self.callbacks += 1
        if code in self.codes:
            self.hits[code].add(line)
        return MON.DISABLE

    def start(self) -> None:
        super().start()
        MON.register_callback(self.tool_id, EV.LINE, self._line)
        for code in self.codes:
            MON.set_local_events(self.tool_id, code, EV.LINE)

    def relative(self, code: CodeType) -> list[int]:
        return sorted(line - code.co_firstlineno for line in self.hits[code])


class BranchCoverage(Tool):
    """Arc coverage from BRANCH_LEFT and BRANCH_RIGHT, reported as line pairs."""

    def __init__(self, code: CodeType) -> None:
        super().__init__(MON.COVERAGE_ID, "minitrace-branch")
        self.code = code
        self.arcs: set[tuple[str, int, int]] = set()
        self._line_at = {}
        for ins in dis.get_instructions(code):
            if ins.positions and ins.positions.lineno is not None:
                self._line_at[ins.offset] = ins.positions.lineno

    def _rel(self, offset: int) -> int:
        line = self._line_at.get(offset)
        return -1 if line is None else line - self.code.co_firstlineno

    def _make(self, side: str):
        def cb(code: CodeType, src: int, dest: int):
            if code is self.code:
                self.arcs.add((side, self._rel(src), self._rel(dest)))
            return MON.DISABLE

        return cb

    def start(self) -> None:
        super().start()
        MON.register_callback(self.tool_id, EV.BRANCH_LEFT, self._make("left"))
        MON.register_callback(self.tool_id, EV.BRANCH_RIGHT, self._make("right"))
        MON.set_local_events(self.tool_id, self.code, EV.BRANCH_LEFT | EV.BRANCH_RIGHT)


class CallCounter(Tool):
    """Deterministic call counts from PY_START, with call depth from PY_RETURN and PY_UNWIND.

    Events are switched on while frames are already running (start() and
    __enter__ itself), and switched off while others are running (__exit__ and
    stop()). Those frames produce a return with no start, or a start with no
    return, so the tool keeps a shadow stack and reports the mismatches instead
    of letting them corrupt the depth.
    """

    def __init__(self) -> None:
        super().__init__(MON.PROFILER_ID, "minitrace-calls")
        self.calls: collections.Counter[str] = collections.Counter()
        self.stack: list[CodeType] = []
        self.max_depth = 0
        self.unmatched_returns: list[str] = []
        # The counter a first draft would write: +1 on start, -1 on leave.
        # Kept only so the lab can print how far it drifts from the shadow stack.
        self.naive_depth = 0
        self.naive_max = 0
        self.naive_min = 0

    def _start(self, code: CodeType, offset: int):
        self.calls[code.co_qualname] += 1
        self.stack.append(code)
        self.max_depth = max(self.max_depth, len(self.stack))
        self.naive_depth += 1
        self.naive_max = max(self.naive_max, self.naive_depth)

    def _leave(self, code: CodeType, offset: int, arg: object):
        self.naive_depth -= 1
        self.naive_min = min(self.naive_min, self.naive_depth)
        if self.stack and self.stack[-1] is code:
            self.stack.pop()
        else:
            self.unmatched_returns.append(code.co_qualname)

    def start(self) -> None:
        super().start()
        MON.register_callback(self.tool_id, EV.PY_START, self._start)
        MON.register_callback(self.tool_id, EV.PY_RETURN, self._leave)
        MON.register_callback(self.tool_id, EV.PY_UNWIND, self._leave)
        MON.set_events(self.tool_id, EV.PY_START | EV.PY_RETURN | EV.PY_UNWIND)


class Breakpoints(Tool):
    """A breakpoint engine: LINE events only on the target code object, and
    every line that is not a breakpoint is DISABLEd after its first hit, so
    steady-state cost is one callback per breakpoint hit."""

    def __init__(self, code: CodeType, rel_lines: list[int], watch: str) -> None:
        super().__init__(MON.DEBUGGER_ID, "minitrace-bp")
        self.code = code
        self.lines = {code.co_firstlineno + r for r in rel_lines}
        self.watch = watch
        self.hits: list[tuple[int, object]] = []
        self.callbacks = 0

    def _line(self, code: CodeType, line: int):
        self.callbacks += 1
        if code is not self.code or line not in self.lines:
            return MON.DISABLE
        frame = sys._getframe(1)
        self.hits.append((line - code.co_firstlineno, frame.f_locals.get(self.watch)))
        return None

    def start(self) -> None:
        super().start()
        MON.register_callback(self.tool_id, EV.LINE, self._line)
        MON.set_local_events(self.tool_id, self.code, EV.LINE)


def settrace_line_coverage(func, *args) -> set[int]:
    """Reference implementation with the legacy sys.settrace hook."""
    code = func.__code__
    seen: set[int] = set()

    def local(frame, event, arg):
        if event == "line":
            seen.add(frame.f_lineno - code.co_firstlineno)
        return local

    def global_(frame, event, arg):
        return local if frame.f_code is code else None

    sys.settrace(global_)
    try:
        func(*args)
    finally:
        sys.settrace(None)
    return seen


def event_names(mask: int) -> list[str]:
    """Decode an event mask into names, lowest bit first; the deprecated BRANCH alias is never stored."""
    names = sorted(
        (n for n in dir(EV) if n.isupper() and n not in ("NO_EVENTS", "BRANCH")), key=lambda n: getattr(EV, n)
    )
    return [n for n in names if mask & getattr(EV, n)]


def instrumented_opcodes(code: CodeType) -> str:
    """The live INSTRUMENTED_* opcodes of a code object, which co_code hides."""
    ops = [i.opname for i in dis.get_instructions(code, adaptive=True) if i.opname.startswith("INSTRUMENTED_")]
    if not ops:
        return "none"
    return ", ".join(f"{name} x{n}" for name, n in sorted(collections.Counter(ops).items()))


def who_is_monitoring(code: CodeType | None = None) -> list[str]:
    """Everything the public API can say about active hooks, one line each.

    This is the first thing to print when a process is slow under an unknown
    tool or when use_tool_id() fails. sys.settrace and sys.setprofile hold the
    hidden ids 7 and 6, which no sys.monitoring call reports, so they are read
    from sys.gettrace() and sys.getprofile(); the instrumented opcodes of a
    code object show the union of all of them.
    """
    out = []
    for tool_id in range(6):
        name = MON.get_tool(tool_id)
        if name is None:
            continue
        line = f"tool {tool_id} {name!r}: global={event_names(MON.get_events(tool_id))}"
        if code is not None:
            line += f" local[{code.co_name}]={event_names(MON.get_local_events(tool_id, code))}"
        out.append(line)
    out.append(
        f"sys.gettrace() set: {sys.gettrace() is not None} | sys.getprofile() set: {sys.getprofile() is not None}"
    )
    if code is not None:
        out.append(f"instrumented opcodes in {code.co_name}: {instrumented_opcodes(code)}")
    return out
