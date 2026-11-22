"""tracemalloc helpers: folded allocation stacks for a flamegraph and per-call peak attribution."""

from __future__ import annotations

import tracemalloc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def folded_allocation_stacks(snapshot: tracemalloc.Snapshot, *, min_bytes: int = 1) -> list[str]:
    """Render a snapshot as folded stacks (``root;...;leaf bytes``) for flamegraph.pl.

    ``tracemalloc.Frame`` carries only a filename and a line number, so a frame is rendered
    ``file:lineno``.
    ``Traceback`` is oldest frame first, which is already root-first for a flamegraph.
    """
    lines: list[str] = []
    for stat in snapshot.statistics("traceback"):
        if stat.size < min_bytes:
            continue
        stack = ";".join(f"{frame.filename}:{frame.lineno}" for frame in stat.traceback)
        lines.append(f"{stack} {stat.size}")
    return sorted(lines)


def measure_call[T](func: Callable[[], T]) -> tuple[T, int, int]:
    """Run ``func`` and return ``(result, retained_bytes, peak_bytes)`` of traced memory.

    ``reset_peak`` makes the peak local to the call; tracemalloc must already be tracing.
    Both numbers are process-wide, so they are only attributable when nothing else allocates.
    """
    baseline, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    result = func()
    current, peak = tracemalloc.get_traced_memory()
    return result, current - baseline, peak - baseline
