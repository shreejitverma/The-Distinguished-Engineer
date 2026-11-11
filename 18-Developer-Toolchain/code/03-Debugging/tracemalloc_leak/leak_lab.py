"""Leak isolation lab: tracemalloc snapshot diffs, allocator domains, and the cyclic GC.

Each section prints derived, deterministic facts (counts, line numbers, booleans), never
addresses, timings, or absolute RSS values. Every check is chosen so that it would print
something different if the mechanism it demonstrates were absent.

The golden file is recorded on a GIL build of CPython 3.15. A free-threaded build runs every
section too, and prints a different section 8, because its collector has no generations.

Run through run.sh, which uses `python3 -I`.
"""

from __future__ import annotations

import collections
import ctypes
import functools
import gc
import glob
import itertools
import os
import resource
import subprocess
import sys
import sysconfig
import tempfile
import traceback
import tracemalloc
import types
import weakref

THIS_FILE = os.path.abspath(__file__)
BASENAME = os.path.basename(THIS_FILE)
MIB = 1024 * 1024
FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def header(title: str) -> None:
    print()
    print(f"=== {title} ===")


def only_this_file(snapshot: tracemalloc.Snapshot) -> tracemalloc.Snapshot:
    """Keep traces whose most recent frame is in this file (domain 0 only)."""
    # Filter takes an fnmatch pattern, so a path containing [, *, or ? must be escaped.
    return snapshot.filter_traces([tracemalloc.Filter(True, glob.escape(THIS_FILE))])


# ---------------------------------------------------------------------------
# Section 1: a growing cache found by a snapshot diff
# ---------------------------------------------------------------------------

_CACHE: dict[int, bytes] = {}


def render(request_id: int) -> bytes:
    return bytes(1000) + request_id.to_bytes(8, "little")  # LEAK-PAYLOAD


def cache_middleware(request_id: int) -> bytes:
    body = render(request_id)
    _CACHE[request_id] = body  # LEAK-INSERT
    return body


def handle_request(request_id: int) -> int:
    return len(cache_middleware(request_id))


def line_of(marker: str) -> int:
    with open(THIS_FILE, encoding="ascii") as f:
        for number, text in enumerate(f, start=1):
            if text.rstrip().endswith(f"# {marker}"):
                return number
    raise LookupError(marker)


def section_snapshot_diff() -> None:
    header("1. Snapshot diff finds the growing cache")
    tracemalloc.start(10)
    gc.collect()
    before = tracemalloc.take_snapshot()
    for i in range(1000):
        handle_request(i)  # REQUEST-LOOP
    after = tracemalloc.take_snapshot()

    mine_before, mine_after = only_this_file(before), only_this_file(after)
    stats = mine_after.compare_to(mine_before, "lineno")
    payload_line = line_of("LEAK-PAYLOAD")
    insert_line = line_of("LEAK-INSERT")
    top = stats[0]
    print(
        f"Top growth line is the payload line ({BASENAME}:{payload_line}): "
        f"{top.traceback[0].lineno == payload_line}"
    )
    payload_size = sys.getsizeof(_CACHE[0])
    sizes = [t.size for t in mine_after.traces if t.traceback[-1].lineno == payload_line]
    exact = sum(1 for n in sizes if n == payload_size)
    print(f"  blocks of exactly sys.getsizeof(payload) = {payload_size} bytes: {exact}")
    # The diff may also carry a few free-list blocks last used on that line (an argument
    # tuple, a bytes writer); their number is an interpreter detail, so only bound it.
    extra_blocks = top.count_diff - exact
    extra_bytes = top.size_diff - exact * payload_size
    print(
        f"  count_diff and size_diff equal {exact} blocks and {exact * payload_size} bytes, "
        f"within 4 blocks and 1024 bytes: {0 <= extra_blocks <= 4 and 0 <= extra_bytes <= 1024}"
    )
    gc.collect()  # a full collection empties the free lists
    settled = only_this_file(tracemalloc.take_snapshot())
    on_line = sum(1 for t in settled.traces if t.traceback[-1].lineno == payload_line)
    print(f"  after gc.collect() empties the free lists, blocks on that line: {on_line}")
    insert = [s for s in stats if s.traceback[0].lineno == insert_line]
    print(
        f"Dict growth charged to the insert line ({BASENAME}:{insert_line}): "
        f"{bool(insert) and insert[0].size_diff > 0}"
    )
    loop_line = line_of("REQUEST-LOOP")
    plain = {s.traceback[0].lineno: s.count for s in mine_after.statistics("lineno")}
    summed = {s.traceback[0].lineno: s.count for s in mine_after.statistics("lineno", cumulative=True)}
    print(
        f"cumulative=True also charges the payloads to the calling line ({BASENAME}:{loop_line}): "
        f"{summed.get(loop_line, 0) >= 1000 > plain.get(loop_line, 0)}"
    )

    # Group by full traceback to recover the call path of the leaking allocation.
    by_tb = mine_after.compare_to(mine_before, "traceback")
    tb = by_tb[0].traceback
    payload_traces = (
        t for t in mine_after.traces if t.traceback[-1].lineno == payload_line and t.size == payload_size
    )
    one = next(payload_traces, None)
    print(
        f"Stored frames for the top trace: {len(tb)} (limit {tracemalloc.get_traceback_limit()}), "
        f"Trace.traceback.total_nframe: {one.traceback.total_nframe if one else 'no payload trace'}"
    )
    print("Call path, oldest first:")
    with open(THIS_FILE, encoding="ascii") as f:
        lines = f.read().splitlines()
    for fr in tb:
        print(f"  {BASENAME}:{fr.lineno}: {lines[fr.lineno - 1].strip()}")

    # get_object_traceback maps one live object back to its allocation site.
    obj_tb = tracemalloc.get_object_traceback(_CACHE[500])
    print(
        f"get_object_traceback(_CACHE[500]) points at the payload line: "
        f"{obj_tb is not None and obj_tb[-1].lineno == payload_line}"
    )

    # A snapshot is a plain file: dump it in the process, diff it anywhere.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "after.snapshot")
        after.dump(path)
        loaded = only_this_file(tracemalloc.Snapshot.load(path))
    again = loaded.compare_to(mine_before, "lineno")[0]
    same = (again.traceback[0].lineno, again.count_diff, again.size_diff) == (
        top.traceback[0].lineno,
        top.count_diff,
        top.size_diff,
    )
    print(f"Snapshot.dump() then Snapshot.load() gives the same top entry: {same}")

    _CACHE.clear()
    gc.collect()
    final = only_this_file(tracemalloc.take_snapshot())
    remaining = [s for s in final.compare_to(mine_before, "lineno") if s.traceback[0].lineno == payload_line]
    print(f"After _CACHE.clear(), payload blocks still traced: {remaining[0].count if remaining else 0}")
    tracemalloc.stop()


# ---------------------------------------------------------------------------
# Section 2: which allocations tracemalloc can see
# ---------------------------------------------------------------------------


def traced_bytes_in_domain(snapshot: tracemalloc.Snapshot, domain: int) -> int:
    filtered = snapshot.filter_traces([tracemalloc.DomainFilter(True, domain)])
    return sum(t.size for t in filtered.traces)


def section_domains() -> None:
    header("2. Allocator domains: what the hooks see")
    libc = ctypes.CDLL(None)
    libc.malloc.restype = ctypes.c_void_p
    libc.malloc.argtypes = [ctypes.c_size_t]
    libc.free.argtypes = [ctypes.c_void_p]
    api = ctypes.pythonapi
    api.PyMem_RawMalloc.restype = ctypes.c_void_p
    api.PyMem_RawMalloc.argtypes = [ctypes.c_size_t]
    api.PyMem_RawFree.argtypes = [ctypes.c_void_p]
    api.PyTraceMalloc_Track.restype = ctypes.c_int
    api.PyTraceMalloc_Track.argtypes = [ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
    api.PyTraceMalloc_Untrack.restype = ctypes.c_int
    api.PyTraceMalloc_Untrack.argtypes = [ctypes.c_uint, ctypes.c_size_t]

    size = 8 * MIB
    custom_domain = 4242

    print(f"PyTraceMalloc_Track before start() returns: {api.PyTraceMalloc_Track(custom_domain, 1, 1)}")
    tracemalloc.start(1)
    base_current, _ = tracemalloc.get_traced_memory()

    raw = api.PyMem_RawMalloc(size)  # RAW-ALLOC
    after_raw, _ = tracemalloc.get_traced_memory()
    snap = only_this_file(tracemalloc.take_snapshot())
    raw_line_bytes = sum(
        s.size for s in snap.statistics("lineno") if s.traceback[0].lineno == line_of("RAW-ALLOC")
    )
    print(f"PyMem_RawMalloc({size // MIB} MiB) traced in domain 0: {after_raw - base_current >= size}")
    print(
        f"  charged to the calling Python line ({BASENAME}:{line_of('RAW-ALLOC')}): "
        f"{size <= raw_line_bytes < size + 4096}"
    )

    native = libc.malloc(size)  # NATIVE-ALLOC
    after_native, _ = tracemalloc.get_traced_memory()
    print(f"libc malloc({size // MIB} MiB) traced: {after_native - after_raw >= size}")

    rc = api.PyTraceMalloc_Track(custom_domain, native, size)
    snap = tracemalloc.take_snapshot()
    print(f"PyTraceMalloc_Track(domain={custom_domain}) returns: {rc}")
    print(f"  bytes in domain {custom_domain}: {traced_bytes_in_domain(snap, custom_domain) // MIB} MiB")
    print(
        f"  get_traced_memory() now includes it: {tracemalloc.get_traced_memory()[0] - after_native >= size}"
    )
    print(f"PyTraceMalloc_Untrack returns: {api.PyTraceMalloc_Untrack(custom_domain, native)}")
    snap = tracemalloc.take_snapshot()
    print(f"  bytes in domain {custom_domain} after untrack: {traced_bytes_in_domain(snap, custom_domain)}")

    libc.free(native)
    api.PyMem_RawFree(raw)
    after_free, _ = tracemalloc.get_traced_memory()
    print(f"PyMem_RawFree removes the trace: {after_free < after_raw - size // 2}")
    own_before = tracemalloc.get_tracemalloc_memory()
    traced_before, _ = tracemalloc.get_traced_memory()
    blocks = [bytes(64) for _ in range(10_000)]
    own_growth = tracemalloc.get_tracemalloc_memory() - own_before
    traced_growth = tracemalloc.get_traced_memory()[0] - traced_before
    print(
        f"{len(blocks)} more traced blocks grow tracemalloc's own tables by at least 16 bytes each: "
        f"{own_growth >= 16 * len(blocks)}"
    )
    print(
        f"  get_traced_memory() counts the blocks but not those tables: "
        f"{traced_growth < len(blocks) * sys.getsizeof(blocks[0]) + sys.getsizeof(blocks) + 4096}"
    )
    del blocks

    tracemalloc.reset_peak()
    base, _ = tracemalloc.get_traced_memory()
    scratch = bytearray(4 * MIB)
    del scratch
    current, peak = tracemalloc.get_traced_memory()
    print(f"A freed 4 MiB temporary leaves current within 1 MiB of its old value: {current - base < MIB}")
    print(f"  but the peak is at least 4 MiB higher: {peak - base >= 4 * MIB}")
    tracemalloc.reset_peak()
    current, peak = tracemalloc.get_traced_memory()
    print(f"  after reset_peak(), the peak is within 1 MiB of current again: {peak - current < MIB}")
    tracemalloc.stop()
    print(f"PyTraceMalloc_Untrack after stop() returns: {api.PyTraceMalloc_Untrack(custom_domain, native)}")


def rusage_peak_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and KiB on Linux.
    return peak if sys.platform == "darwin" else peak * 1024


def rss_peak_bytes() -> int:
    """Peak resident set size of this process image, in bytes."""
    if sys.platform != "linux":
        return rusage_peak_bytes()
    # On Linux, execve() folds the peak of the image it replaces into ru_maxrss, so a child
    # of a larger parent starts at the parent's peak. VmHWM belongs to this image alone.
    with open("/proc/self/status", encoding="ascii") as f:
        for line in f:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    raise LookupError("VmHWM")


def native_child() -> None:
    """Child process: leak 64 MiB with libc malloc and touch every page."""
    libc = ctypes.CDLL(None)
    libc.malloc.restype = ctypes.c_void_p
    libc.malloc.argtypes = [ctypes.c_size_t]
    inherited = rusage_peak_bytes() >= 96 * MIB
    tracemalloc.start()
    traced_before = tracemalloc.get_traced_memory()[0]
    rss_before = rss_peak_bytes()
    size = 64 * MIB
    ptr = libc.malloc(size)
    ctypes.memset(ptr, 1, size)
    traced_growth = tracemalloc.get_traced_memory()[0] - traced_before
    rss_growth = rss_peak_bytes() - rss_before
    print(f"Native leak of 64 MiB: tracemalloc growth under 1 MiB: {traced_growth < MIB}")
    print(f"Native leak of 64 MiB: peak RSS growth at least 60 MiB: {rss_growth >= 60 * MIB}")
    print(f"Child's first ru_maxrss reading already holds the parent's 128 MiB peak: {inherited}")


def section_native_leak() -> None:
    header("3. A native leak is invisible to tracemalloc but not to RSS")
    ballast = b"\x01" * (128 * MIB)  # written pages, so this process peaks above 128 MiB
    del ballast
    out = subprocess.run(
        [sys.executable, "-I", THIS_FILE, "native-child"], capture_output=True, text=True, check=True
    )
    print(out.stdout, end="")


# ---------------------------------------------------------------------------
# Section 4: reference cycles and the cyclic collector
# ---------------------------------------------------------------------------


class Node:
    __slots__ = ("__weakref__", "peer")

    def __init__(self) -> None:
        self.peer: Node | None = None


def section_cycles() -> None:
    header("4. Reference cycles need the cyclic GC")
    gc.collect()
    gc.disable()
    refs = []
    for _ in range(1000):
        a, b = Node(), Node()
        a.peer, b.peer = b, a
        refs.append(weakref.ref(a))
    del a, b
    alive = sum(r() is not None for r in refs)
    print(f"Pairs alive after the last strong name is gone, GC disabled: {alive}")
    found = gc.collect()
    alive = sum(r() is not None for r in refs)
    print(f"gc.collect() returned: {found}")
    print(f"Pairs alive after gc.collect(): {alive}")

    # The same pair without a cycle is freed by reference counting alone.
    a, b = Node(), Node()
    a.peer = b
    r = weakref.ref(b)
    del a, b
    print(f"Acyclic pair freed by refcounting with GC disabled: {r() is None}")
    gc.enable()

    print(
        f"gc.is_tracked: Node() {gc.is_tracked(Node())}, int {gc.is_tracked(10**30)}, "
        f"str {gc.is_tracked('abc')}, list {gc.is_tracked([])}, "
        f"empty dict {gc.is_tracked({})}"
    )


# ---------------------------------------------------------------------------
# Section 5: finding who holds a leaked object with gc.get_referrers
# ---------------------------------------------------------------------------

LISTENERS: list = []


class Session:
    def __init__(self, sid: int) -> None:
        self.sid = sid
        self.buffer = bytearray(10_000)

    def on_event(self, event: str) -> None:
        pass


def describe(obj: object) -> str:
    for name, value in globals().items():
        if value is obj:
            return f"module global {name!r}"
    return type(obj).__name__


def section_referrers() -> None:
    header("5. gc.get_referrers walks back to the root")
    s = Session(7)
    LISTENERS.append(s.on_event)
    r = weakref.ref(s)
    del s
    gc.collect()
    print(f"Session alive after del and gc.collect(): {r() is not None}")
    # Walk referrers until a module global is reached, ignoring this frame's own lists.
    chain: list[str] = []
    obj = r()
    for _ in range(4):
        holders = gc.get_referrers(obj)
        ignore = {id(chain), id(holders), id(sys._getframe())}
        candidates = [h for h in holders if id(h) not in ignore]
        del holders
        if not candidates:
            break
        holder = candidates[0]
        del candidates
        chain.append(describe(holder))
        if chain[-1].startswith("module global"):
            break
        obj = holder
    obj = holder = None
    print("Referrer chain from the session: " + " <- ".join(["Session"] + chain))

    LISTENERS.clear()
    print(f"Session freed after the bound method is removed: {r() is None}")

    s = Session(8)
    LISTENERS.append(weakref.WeakMethod(s.on_event))
    r = weakref.ref(s)
    del s
    print(f"With weakref.WeakMethod, session freed at once: {r() is None}")
    print(f"  dead WeakMethod returns None: {LISTENERS[0]() is None}")
    LISTENERS.clear()


# ---------------------------------------------------------------------------
# Section 6: an exception that pins a frame
# ---------------------------------------------------------------------------


class Big:
    def __init__(self) -> None:
        self.data = bytearray(1 * MIB)


LAST_ERROR: BaseException | None = None


def failing_job() -> weakref.ref:
    big = Big()
    ref = weakref.ref(big)
    try:
        raise ValueError("job failed")
    except ValueError as exc:
        global LAST_ERROR
        LAST_ERROR = exc
    return ref


def section_exception_frames() -> None:
    global LAST_ERROR
    header("6. A saved exception pins its frames")
    ref = failing_job()
    print(f"Local 'big' alive after the function returned: {ref() is not None}")
    frame = LAST_ERROR.__traceback__.tb_frame
    print(f"  reachable as exc.__traceback__.tb_frame.f_locals['big']: {frame.f_locals.get('big') is ref()}")
    del frame
    traceback.clear_frames(LAST_ERROR.__traceback__)
    print(f"After traceback.clear_frames(): {'freed' if ref() is None else 'alive'}")
    LAST_ERROR = None

    gc.collect()
    gc.disable()

    def self_cycle() -> weakref.ref:
        big = Big()
        ref = weakref.ref(big)
        try:
            raise ValueError("x")
        except ValueError as exc:
            err = exc  # noqa: F841 - the cycle: frame -> err -> traceback -> frame
        return ref

    ref = self_cycle()
    print(f"Exception kept in a local, GC disabled: 'big' alive after return: {ref() is not None}")
    gc.collect()
    print(f"  after gc.collect(): {'freed' if ref() is None else 'alive'}")
    gc.enable()


# ---------------------------------------------------------------------------
# Section 7: finalizers, gc.garbage, DEBUG_SAVEALL
# ---------------------------------------------------------------------------

FINALIZED: list[int] = []


class WithDel:
    def __init__(self, n: int) -> None:
        self.n = n
        self.me = self

    def __del__(self) -> None:
        FINALIZED.append(self.n)


def section_finalizers() -> None:
    header("7. Finalizers in cycles (PEP 442) and DEBUG_SAVEALL")
    gc.collect()
    for i in range(5):
        WithDel(i)
    print(f"__del__ calls before the collector runs: {sorted(FINALIZED)}")
    gc.collect()
    print(f"__del__ ran for cyclic objects: {sorted(FINALIZED)}, gc.garbage size: {len(gc.garbage)}")

    gc.set_debug(gc.DEBUG_SAVEALL)
    for i in range(5):
        WithDel(100 + i)
    gc.collect()
    saved = sum(isinstance(o, WithDel) for o in gc.garbage)
    print(f"With DEBUG_SAVEALL: WithDel objects kept in gc.garbage: {saved}")
    gc.set_debug(0)
    gc.garbage.clear()
    gc.collect()
    print(f"gc.garbage after clear: {len(gc.garbage)}")


# ---------------------------------------------------------------------------
# Section 8: the generational trigger
# ---------------------------------------------------------------------------


def count_collections(n_objects: int, keep_them: bool) -> tuple[list[int], list[int]]:
    """Create n_objects lists; return collections per generation and full-run candidates."""
    seen = [0, 0, 0]
    candidates: list[int] = []

    def on_gc(phase: str, info: dict) -> None:
        if phase == "start":
            seen[info["generation"]] += 1
        elif info["generation"] == 2 and "candidates" in info:
            candidates.append(info["candidates"])

    gc.collect()
    keep = []
    gc.callbacks.append(on_gc)
    if keep_them:
        for _ in range(n_objects):
            keep.append([])
    else:
        for _ in range(n_objects):
            [].append([])
    gc.callbacks.remove(on_gc)
    del keep
    gc.collect()
    return seen, candidates


def section_generations() -> None:
    header("8. Generational trigger and the 25 percent rule")
    thresholds = gc.get_threshold()
    print(f"gc.get_threshold(): {thresholds}")
    n = 1_000_000
    seen, _ = count_collections(n, keep_them=True)
    if FREE_THREADED:
        # One non-generational collector: every automatic run reports generation 0, and the
        # generation arithmetic of the default build does not apply.
        print("Free-threaded build: the collector has no generations")
        print(f"  every automatic run reports generation 0: {seen[1] + seen[2] == 0}")
        print(f"  runs for {n} surviving lists are fewer than n // (threshold0 + 1): {sum(seen) < n // 2001}")
        seen, _ = count_collections(n, keep_them=False)
        print(f"{n} lists freed at once by refcounting: collections: {sum(seen)}")
        return
    print(f"{n} surviving lists: collections by generation (0, 1, 2): {tuple(seen)}")
    print(f"  total equals n // (threshold0 + 1): {sum(seen) == n // 2001}")
    older = seen[1] + seen[2]
    print(f"  one run in 11 to 12 reaches generation 1 or 2: {older > 0 and 11 <= sum(seen) / older <= 12}")
    print(
        f"  one generation 1-or-older run in 11 to 15 is a full run: "
        f"{seen[2] > 0 and 11 <= older / seen[2] <= 15}"
    )
    seen, _ = count_collections(n, keep_them=False)
    print(f"{n} lists freed at once by refcounting: collections: {sum(seen)}")

    # threshold2 = 0 makes every run after a generation 1 run eligible for a full
    # collection, so only the long_lived_pending >= long_lived_total / 4 test limits it.
    gc.set_threshold(2000, 10, 0)
    seen, candidates = count_collections(n, keep_them=True)
    gc.set_threshold(*thresholds)
    print(f"With set_threshold(2000, 10, 0): collections by generation: {tuple(seen)}")
    if len(candidates) >= 2:
        growth = [b / a for a, b in itertools.pairwise(candidates)]
        print(f"  each full run scanned at least 1.25x the previous one: {min(growth) >= 1.25}")
        print(
            f"  full runs fewer than half the eligible generation 1 slots: {seen[2] * 2 < seen[1] + seen[2]}"
        )
    else:
        print("  gc.callbacks info has no 'candidates' key on this version")


# ---------------------------------------------------------------------------
# Section 9: free lists and the reference tracer
# ---------------------------------------------------------------------------


def blocks_on(marker: str) -> int:
    """Traced blocks whose most recent frame is the line carrying `marker`."""
    line = line_of(marker)
    snapshot = only_this_file(tracemalloc.take_snapshot())
    return sum(1 for t in snapshot.traces if t.traceback[-1].lineno == line)


def section_free_lists() -> None:
    header("9. Free lists keep blocks traced, and reuse moves the traceback")
    tracemalloc.start(1)
    gc.collect()  # empty free lists, so the next float needs a new block
    first = float("1.5")  # FLOAT-FIRST
    first_id = id(first)
    del first
    print(f"Float freed into the free list: blocks still traced on its line: {blocks_on('FLOAT-FIRST')}")
    second = float("2.5")  # FLOAT-SECOND
    second_line = line_of("FLOAT-SECOND")
    tb = tracemalloc.get_object_traceback(second)
    print(f"Next float reuses the same block (same id()): {id(second) == first_id}")
    print(
        f"  the block is now charged to the reusing line ({BASENAME}:{second_line}): "
        f"{tb is not None and tb[-1].lineno == second_line}"
    )
    print(f"  blocks left on the first line: {blocks_on('FLOAT-FIRST')}")
    del second
    print(f"Second float freed: blocks still traced on its line: {blocks_on('FLOAT-SECOND')}")
    gc.collect()  # FREELIST-COLLECT
    print(f"  after gc.collect(): {blocks_on('FLOAT-SECOND')}")
    tracemalloc.stop()


# ---------------------------------------------------------------------------
# Section 10: a reference count that nothing owns
# ---------------------------------------------------------------------------


class Record:
    __slots__ = ("__weakref__", "payload")

    def __init__(self) -> None:
        self.payload = bytearray(100_000)


def section_refcount_leak() -> None:
    header("10. A leaked reference count has no referrer")
    api = ctypes.pythonapi
    api.Py_IncRef.argtypes = [ctypes.py_object]
    api.Py_IncRef.restype = None
    api.Py_DecRef.argtypes = [ctypes.py_object]
    api.Py_DecRef.restype = None

    tracemalloc.start(1)
    record = Record()  # RECORD-ALLOC
    ref = weakref.ref(record)
    api.Py_IncRef(record)  # what a C extension does when it forgets Py_DECREF
    del record
    gc.collect()
    print(f"Object alive after del and gc.collect(), one Py_IncRef unmatched: {ref() is not None}")
    holders = [h for h in gc.get_referrers(ref()) if not isinstance(h, types.FrameType)]
    print(f"  gc.get_referrers finds a holder: {bool(holders)}")
    del holders
    tb = tracemalloc.get_object_traceback(ref())
    print(
        f"  tracemalloc still names the allocating line ({BASENAME}:{line_of('RECORD-ALLOC')}): "
        f"{tb is not None and tb[-1].lineno == line_of('RECORD-ALLOC')}"
    )
    del tb
    api.Py_DecRef(ref())
    print(f"After the matching Py_DecRef: {'freed' if ref() is None else 'alive'}")
    tracemalloc.stop()


# ---------------------------------------------------------------------------
# Section 11: a census of tracked objects by type
# ---------------------------------------------------------------------------

LEAKED_ORDERS: list = []
LEAKED_BLOBS: list = []


class Order:
    __slots__ = ("oid",)

    def __init__(self, oid: int) -> None:
        self.oid = oid


def census() -> collections.Counter:
    return collections.Counter(type(o).__name__ for o in gc.get_objects())


def section_census() -> None:
    header("11. A census by type sees only GC-tracked objects")
    gc.collect()
    before = census()
    LEAKED_ORDERS.extend(Order(i) for i in range(500))
    name, added = (census() - before).most_common(1)[0]
    print(f"Largest growth in gc.get_objects() by type after 500 leaked orders: {name} +{added}")

    tracemalloc.start(1)
    before = census()
    LEAKED_BLOBS.extend(bytes(100) + i.to_bytes(4, "little") for i in range(500))  # BLOB-ALLOC
    after = census()
    kind = type(LEAKED_BLOBS[0]).__name__
    blob_size = sys.getsizeof(LEAKED_BLOBS[0])
    blob_line = line_of("BLOB-ALLOC")
    snapshot = only_this_file(tracemalloc.take_snapshot())
    traced = sum(1 for t in snapshot.traces if t.traceback[-1].lineno == blob_line and t.size == blob_size)
    print(f"500 leaked {kind} objects: growth in the census: {after[kind] - before[kind]}")
    print(f"  blocks of their size traced on their line: {traced}")
    tracemalloc.stop()
    LEAKED_ORDERS.clear()
    LEAKED_BLOBS.clear()


# ---------------------------------------------------------------------------
# Section 12: functools.lru_cache on a method
# ---------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.rows = bytearray(1 * MIB)

    @functools.lru_cache(maxsize=128)  # noqa: B019 - the leak this section demonstrates
    def total(self, column: int) -> int:
        return len(self.rows) + column


def section_lru_cache() -> None:
    header("12. functools.lru_cache on a method pins self")
    report = Report()
    report.total(3)
    ref = weakref.ref(report)
    del report
    gc.collect()
    print(f"Report alive after del and gc.collect(): {ref() is not None}")
    print(f"  cache_info().currsize: {Report.total.cache_info().currsize}")
    Report.total.cache_clear()
    print(f"After cache_clear(): {'freed' if ref() is None else 'alive'}")


# ---------------------------------------------------------------------------
# Section 13: ResourceWarning and tracemalloc
# ---------------------------------------------------------------------------


def unclosed_file_child() -> None:
    """Child process: drop a file object without closing it."""
    handle = open(THIS_FILE, encoding="ascii")  # noqa: SIM115  # UNCLOSED-OPEN
    del handle


def resource_warning(*interpreter_options: str) -> str:
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-W",
            "always::ResourceWarning",
            *interpreter_options,
            THIS_FILE,
            "unclosed-file",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stderr


def section_resource_warning() -> None:
    header("13. ResourceWarning names the allocation site only while tracing")
    plain = resource_warning()
    print(
        f"Tracing off: warning for the unclosed file: {'ResourceWarning: unclosed file' in plain}, "
        f"tells you to enable tracemalloc: {'Enable tracemalloc to get the object allocation traceback' in plain}"
    )
    traced = resource_warning("-X", "tracemalloc=5")
    open_line = line_of("UNCLOSED-OPEN")
    print(
        f"With -X tracemalloc=5: 'Object allocated at' with the open() line ({BASENAME}:{open_line}): "
        f"{'Object allocated at (most recent call last):' in traced and f'lineno {open_line}' in traced}"
    )


def main() -> None:
    gil = "free-threaded" if FREE_THREADED else "GIL"
    print(f"CPython {sys.version_info.major}.{sys.version_info.minor} ({gil} build)")
    section_snapshot_diff()
    section_domains()
    section_native_leak()
    section_cycles()
    section_referrers()
    section_exception_frames()
    section_finalizers()
    section_generations()
    section_free_lists()
    section_refcount_leak()
    section_census()
    section_lru_cache()
    section_resource_warning()


if __name__ == "__main__":
    if sys.argv[1:] == ["native-child"]:
        native_child()
    elif sys.argv[1:] == ["unclosed-file"]:
        unclosed_file_child()
    else:
        main()
