"""Run a coroutine under ``cProfile`` and expose per-function cumulative time and call counts."""

from __future__ import annotations

import asyncio
import cProfile
import pstats
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


@dataclass(frozen=True, slots=True)
class CProfileView:
    wall_s: float
    cumulative_s: dict[str, float]
    calls: dict[str, int]


def cprofile_coroutine(factory: Callable[[], Coroutine[Any, Any, object]]) -> CProfileView:
    """Profile ``asyncio.run(factory())``; keys are bare function names (last one wins)."""
    profiler = cProfile.Profile()
    start = time.perf_counter()
    profiler.enable()
    try:
        asyncio.run(factory())
    finally:
        profiler.disable()
    wall_s = time.perf_counter() - start
    stats = pstats.Stats(profiler)
    stats.calc_callees()
    cumulative: dict[str, float] = {}
    calls: dict[str, int] = {}
    raw = cast(
        "dict[tuple[str, int, str], tuple[int, int, float, float, object]]",
        getattr(stats, "stats"),  # noqa: B009
    )
    for (_filename, _lineno, name), (_cc, ncalls, _tt, cumtime, _callers) in raw.items():
        cumulative[name] = max(cumulative.get(name, 0.0), cumtime)
        calls[name] = calls.get(name, 0) + ncalls
    return CProfileView(wall_s=wall_s, cumulative_s=cumulative, calls=calls)
