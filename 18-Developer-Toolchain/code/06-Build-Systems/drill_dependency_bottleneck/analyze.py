"""Read one clean Ninja build's .ninja_log and graph, and print schedule facts that survive noise.

    python3 analyze.py BUILD_DIR JOBS WALL_MS_OUT

Inputs:
- BUILD_DIR/.ninja_log, format v7: one line per output with start_ms, end_ms, mtime_ns, output,
  and command_hash, tab-separated; start and end are milliseconds since that ninja run began.
  Outputs of one edge share a line's times and hash, which is how edges are regrouped here.
- `ninja -t query`, followed recursively from the `app` target, for every edge's explicit,
  implicit (`| x`), and order-only (`|| x`) inputs. Phony edges cost nothing and are not logged.

What is printed:
- Graph facts (deterministic): how many compiles have any path to the code generator, and which
  real edges the generator itself waits for.
- A slot model (deterministic): the same graph list-scheduled on JOBS slots with the lab's nominal
  costs (codegen DRILL_CODEGEN_SECONDS, compile DRILL_TU_SECONDS, archive and link 0) and Ninja's
  own priority rule (larger count of non-phony edges on the path to the target goes first).
- Measured facts, printed only as counts or yes/no checks, because wall-clock milliseconds vary
  from run to run. Raw milliseconds go to stderr, and the measured wall time to WALL_MS_OUT.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CODEGEN_OUTPUT = "gen/core/schema.h"
ROOT_TARGET = "app"
MODEL_SLACK_MS = 3500  # real compiler work on top of the nominal sleeps, summed over the waves


@dataclass(eq=False)
class Edge:
    outputs: list[str]
    start: int
    end: int

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def name(self) -> str:
        return min(self.outputs, key=len)


@dataclass
class Node:
    inputs: list[str] = field(default_factory=list)
    order_only: list[str] = field(default_factory=list)
    has_edge: bool = False


def read_log(build: Path) -> dict[str, Edge]:
    lines = (build / ".ninja_log").read_text().splitlines()
    if not lines or lines[0] != "# ninja log v7":
        raise SystemExit(f"analyze.py: expected '# ninja log v7', got {lines[:1]}")
    by_command: dict[tuple[int, int, str], Edge] = {}
    for line in lines[1:]:
        start, end, _mtime, output, command_hash = line.split("\t")
        key = (int(start), int(end), command_hash)
        by_command.setdefault(key, Edge([], int(start), int(end))).outputs.append(output)
    return {output: edge for edge in by_command.values() for output in edge.outputs}


def query(build: Path, targets: list[str]) -> dict[str, Node]:
    text = subprocess.run(
        ["ninja", "-C", str(build), "-t", "query", *targets], check=True, capture_output=True, text=True
    ).stdout
    nodes: dict[str, Node] = {}
    current: Node | None = None
    in_inputs = False
    for raw in text.splitlines():
        if not raw.startswith(" "):
            current = nodes.setdefault(raw.rstrip(":"), Node())
            in_inputs = False
            continue
        stripped = raw.strip()
        if stripped.startswith("input:"):
            in_inputs = True
            assert current is not None
            current.has_edge = True
        elif stripped.endswith(":") and not stripped.startswith("|"):
            in_inputs = False  # outputs: or validations:
        elif in_inputs and current is not None:
            if stripped.startswith("|| "):
                current.order_only.append(stripped[3:])
            elif stripped.startswith("| "):
                current.inputs.append(stripped[2:])
            else:
                current.inputs.append(stripped)
    return nodes


def load_graph(build: Path) -> dict[str, Node]:
    graph: dict[str, Node] = {}
    frontier = [ROOT_TARGET]
    while frontier:
        batch = [t for t in dict.fromkeys(frontier) if t not in graph]
        if not batch:
            break
        graph.update(query(build, batch))
        frontier = [i for t in batch for i in graph[t].inputs + graph[t].order_only]
    return graph


def kind(edge: Edge) -> str:
    out = edge.name
    if out == CODEGEN_OUTPUT:
        return "codegen"
    if out.endswith(".o"):
        return "compile"
    if out.endswith(".a"):
        return "archive"
    if out == ROOT_TARGET:
        return "link"
    return "other"


def prerequisites(graph: dict[str, Node], log: dict[str, Edge]) -> dict[Edge, set[Edge]]:
    """For every logged edge, the logged edges it waits for directly, looking through phony nodes."""
    memo: dict[str, set[Edge]] = {}

    def through(name: str) -> set[Edge]:
        if name in log:
            return {log[name]}
        if name in memo:
            return memo[name]
        node = graph.get(name)
        found: set[Edge] = set()
        if node is not None and node.has_edge:
            for dep in node.inputs + node.order_only:
                found |= through(dep)
        memo[name] = found
        return found

    prereqs: dict[Edge, set[Edge]] = {}
    for name, edge in log.items():
        node = graph.get(name)
        if node is None:
            continue
        deps: set[Edge] = set()
        for dep in node.inputs + node.order_only:
            deps |= through(dep)
        prereqs.setdefault(edge, set()).update(deps - {edge})
    return prereqs


def ancestors(prereqs: dict[Edge, set[Edge]], edge: Edge) -> set[Edge]:
    seen: set[Edge] = set()
    stack = [edge]
    while stack:
        for dep in prereqs.get(stack.pop(), ()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def model_cost(edge: Edge) -> int:
    if kind(edge) == "codegen":
        return int(float(os.environ.get("DRILL_CODEGEN_SECONDS", "3")) * 1000)
    if kind(edge) == "compile":
        return int(float(os.environ.get("DRILL_TU_SECONDS", "1")) * 1000)
    return 0


def ninja_weights(prereqs: dict[Edge, set[Edge]]) -> dict[Edge, int]:
    """Ninja 1.12+ priority: count of non-phony edges on the longest path from the edge to the target."""
    users: dict[Edge, list[Edge]] = {e: [] for e in prereqs}
    for edge, deps in prereqs.items():
        for dep in deps:
            users.setdefault(dep, []).append(edge)
    memo: dict[Edge, int] = {}

    def weight(edge: Edge) -> int:
        if edge not in memo:
            memo[edge] = 1 + max((weight(u) for u in users.get(edge, [])), default=0)
        return memo[edge]

    return {edge: weight(edge) for edge in prereqs}


def simulate(prereqs: dict[Edge, set[Edge]], jobs: int) -> int:
    """Greedy list scheduling, as Ninja does it: never leave a slot idle while an edge is ready."""
    weights = ninja_weights(prereqs)
    done: set[Edge] = set()
    running: list[tuple[int, Edge]] = []
    now = 0
    while len(done) < len(prereqs):
        ready = sorted(
            (e for e in prereqs if e not in done and all(r is not e for _, r in running) and prereqs[e] <= done),
            key=lambda e: (-weights[e], e.name),
        )
        while ready and len(running) < jobs:
            edge = ready.pop(0)
            running.append((now + model_cost(edge), edge))
        running.sort(key=lambda item: (item[0], item[1].name))
        now = running[0][0]
        while running and running[0][0] == now:
            done.add(running.pop(0)[1])
    return now


def longest_path(prereqs: dict[Edge, set[Edge]], cost) -> tuple[int, list[Edge]]:
    memo: dict[Edge, tuple[int, list[Edge]]] = {}

    def visit(edge: Edge) -> tuple[int, list[Edge]]:
        if edge not in memo:
            best: tuple[int, list[Edge]] = (0, [])
            for dep in sorted(prereqs[edge], key=lambda e: e.name):
                candidate = visit(dep)
                if (candidate[0], len(candidate[1])) > (best[0], len(best[1])):
                    best = candidate
            memo[edge] = (best[0] + cost(edge), best[1] + [edge])
        return memo[edge]

    # Ties (zero-cost archives and links) go to the longer chain, then to the first name, so the
    # printed path does not depend on the order in which jobs happened to finish.
    best: tuple[int, list[Edge]] = (-1, [])
    for edge in sorted(prereqs, key=lambda e: e.name):
        candidate = visit(edge)
        if (candidate[0], len(candidate[1])) > (best[0], len(best[1])):
            best = candidate
    return best


def order_only_chain(graph: dict[str, Node], start: str) -> list[str]:
    """Follow the last order-only input from one object file until the generated header or a dead end.

    CMake writes `|| .` for a target whose order-only set is empty; "." is not a real dependency.
    """
    chain = [start]
    node = graph[start]
    while deps := [d for d in node.order_only if d != "."]:
        if CODEGEN_OUTPUT in deps:
            chain.append(CODEGEN_OUTPUT)
            break
        chain.append(deps[-1])
        node = graph.get(deps[-1], Node())
    return chain


def secs(ms: float) -> str:
    return f"{ms / 1000:g} s"


def main() -> int:
    build, jobs, wall_out = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
    log = read_log(build)
    graph = load_graph(build)
    prereqs = prerequisites(graph, log)
    edges = list(prereqs)
    gen = log[CODEGEN_OUTPUT]
    compiles = [e for e in edges if kind(e) == "compile"]
    gated = [e for e in compiles if gen in ancestors(prereqs, e)]
    gen_waits = sorted(d.name for d in prereqs[gen])

    print(f"  edges run: {len(edges)} ({len(compiles)} compiles)")
    print(f"  compiles with a graph path to the code generator: {len(gated)} of {len(compiles)}")
    print(f"  code generator waits for: {', '.join(gen_waits) if gen_waits else 'nothing'}")
    probe = min(e.name for e in compiles if "/net.dir/" in e.name)
    print(f"  order-only chain: {' || '.join(order_only_chain(graph, probe))}")

    model_work = sum(model_cost(e) for e in edges)
    model_t_inf, model_path = longest_path(prereqs, model_cost)
    makespan = simulate(prereqs, jobs)
    print(
        f"  slot model: W {secs(model_work)}, T_inf {secs(model_t_inf)}, W/p {secs(model_work / jobs)},"
        f" makespan at p={jobs}: {secs(makespan)}"
    )
    print(f"  slot model critical path: {' -> '.join(kind(e) for e in model_path)}")

    first = min(e.start for e in edges)
    wall = max(e.end for e in edges) - first
    work = sum(e.duration for e in edges)
    t_inf, _ = longest_path(prereqs, lambda e: e.duration)
    events = []
    for e in edges:
        lo, hi = max(e.start, gen.start), min(e.end, gen.end)
        if e is not gen and lo < hi:
            events += [(lo, 1), (hi, -1)]
    alongside = running = 0
    for _, delta in sorted(events):
        running += delta
        alongside = max(alongside, running)
    print(f"  measured: most jobs running alongside the code generator: {alongside} of {jobs - 1}")
    # Any wait on a compile costs at least DRILL_TU_SECONDS of sleep, so this is a hard floor.
    late = gen.start - first >= 1000
    print(f"  measured: code generator started at least 1 s into the build: {'yes' if late else 'no'}")
    within = makespan - 200 <= wall <= makespan + MODEL_SLACK_MS
    print(f"  measured: wall clock within {secs(MODEL_SLACK_MS)} above the slot model: {'yes' if within else 'NO'}")
    lower, upper = max(work / jobs, t_inf), work / jobs + t_inf
    bounded = lower - 150 <= wall <= upper + 150
    print(f"  measured: max(W/p, T_inf) <= wall <= W/p + T_inf: {'yes' if bounded else 'NO'}")
    wall_out.write_text(f"{wall}\n")
    print(
        f"[raw ms] wall={wall} work={work} t_inf={t_inf} gen={gen.start}..{gen.end}"
        f" lower={lower:.0f} upper={upper:.0f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
