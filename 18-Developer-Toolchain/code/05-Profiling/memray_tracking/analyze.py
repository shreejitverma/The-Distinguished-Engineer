"""Read the capture files that run.sh wrote and print derived, run-independent facts.

Usage: python analyze.py <capture-dir>
Every printed number is a count or a byte size that the workload fixes; addresses, PIDs,
timestamps, and RSS never appear. Exits non-zero if a mechanism stops behaving as the note says.
"""

import collections
import sys
from pathlib import Path

from memray import AllocatorType, FileReader

DEALLOCATORS = {AllocatorType.FREE, AllocatorType.PYMALLOC_FREE, AllocatorType.MUNMAP}


def check(cond, msg):
    if not cond:
        print(f"CHECK FAILED: {msg}", file=sys.stderr)
        sys.exit(1)


def by_function(records, funcs):
    """Sum allocation count and bytes per innermost Python function."""
    out = {f: [0, 0] for f in funcs}
    for rec in records:
        stack = rec.stack_trace()
        fn = stack[0][0] if stack else None
        if fn in out:
            out[fn][0] += rec.n_allocations
            out[fn][1] += rec.size
    return out


def allocator_census(path, func):
    """Count every allocation record made directly by `func`, by allocator, with its sizes."""
    counts = collections.Counter()
    sizes = collections.defaultdict(set)
    for rec in FileReader(path).get_allocation_records():
        kind = AllocatorType(rec.allocator)
        if kind in DEALLOCATORS:
            continue
        stack = rec.stack_trace()
        if stack and stack[0][0] == func:
            counts[kind.name] += 1
            sizes[kind.name].add(rec.size)
    return counts, sizes


def section_small(d):
    print("== memray: what the hooks see for 50,000 small tuples ==")
    results = {}
    for label, name in (("default (pymalloc)", "small_default"),
                        ("--trace-python-allocators", "small_tpa"),
                        ("PYTHONMALLOC=malloc", "small_malloc")):
        path = d / f"{name}.bin"
        meta = FileReader(path).metadata
        counts, sizes = allocator_census(path, "make_small_tuples")
        results[name] = (counts, sizes)
        print(f"  {label}: python_allocator={meta.python_allocator} "
              f"trace_python_allocators={meta.trace_python_allocators}")
        for kind in sorted(counts):
            n = counts[kind]
            shown = n if n < 1000 else f">= {n // 1000 * 1000}"
            only = sorted(sizes[kind])
            size_note = f", every size {only[0]}" if len(only) == 1 else ""
            print(f"    {kind:<16} records: {shown}{size_note}")
    dflt, _ = results["small_default"]
    tpa, _ = results["small_tpa"]
    mal, _ = results["small_malloc"]
    check(dflt["PYMALLOC_MALLOC"] == 0 and dflt["MMAP"] > 0, "default mode sees arenas, not objects")
    check(results["small_default"][1]["MMAP"] == {1 << 20}, "every arena is 1 MiB")
    check(tpa["PYMALLOC_MALLOC"] >= 90_000 and tpa["MMAP"] == 0, "pymalloc tracing sees each object")
    check(mal["MALLOC"] >= 90_000 and mal["PYMALLOC_MALLOC"] == 0, "PYTHONMALLOC=malloc makes objects plain malloc")


def section_churn(d):
    print("== memray: temporary allocations from list growth ==")
    reader = FileReader(d / "churn.bin")
    funcs = ("grow_by_append", "preallocate")
    peak = by_function(reader.get_high_watermark_allocation_records(), funcs)
    for f in funcs:
        print(f"  {f}: live at peak {peak[f][0]} allocation(s), {peak[f][1]} bytes")
    for t in (0, 1):
        tmp = by_function(reader.get_temporary_allocation_records(threshold=t), funcs)
        for f in funcs:
            print(f"  threshold={t} {f}: {tmp[f][0]} temporary, {tmp[f][1]} bytes")
    tmp0 = by_function(reader.get_temporary_allocation_records(threshold=0), funcs)
    check(tmp0["grow_by_append"][0] > 50 and tmp0["preallocate"][0] == 0, "append churns, preallocation does not")
    check(peak["preallocate"] == [1, 8_000_000], "[None] * n is one 8-byte slot per element")


def section_peak(d):
    print("== memray: high-water mark versus the threshold window ==")
    reader = FileReader(d / "peak.bin")
    funcs = ("peak_sequential", "peak_together")
    hwm = by_function(reader.get_high_watermark_allocation_records(), funcs)
    for f in funcs:
        print(f"  at the high-water mark, {f}: {hwm[f][0]} allocation(s), {hwm[f][1]} bytes")
    for t in (0, 1, 2):
        tmp = by_function(reader.get_temporary_allocation_records(threshold=t), funcs)
        print(f"  threshold={t}: peak_sequential {tmp['peak_sequential'][0]}, "
              f"peak_together {tmp['peak_together'][0]} temporary")
    leaked = by_function(reader.get_leaked_allocation_records(), funcs)
    print(f"  leaked: {sum(v[0] for v in leaked.values())} allocation(s)")
    check(hwm["peak_sequential"][0] == 0 and hwm["peak_together"][0] == 3, "peak is the moment all three are live")


def section_leak(d):
    print("== memray: leaks versus temporaries in one function ==")
    for label, name in (("full capture", "leak"), ("--aggregate capture", "leak_agg")):
        reader = FileReader(d / f"{name}.bin")
        leaked = by_function(reader.get_leaked_allocation_records(), ("leak_into_cache",))
        hwm = by_function(reader.get_high_watermark_allocation_records(), ("leak_into_cache",))
        print(f"  {label} ({reader.metadata.file_format.name}): leaked {leaked['leak_into_cache'][0]} "
              f"allocation(s), {leaked['leak_into_cache'][1]} bytes; "
              f"live at peak {hwm['leak_into_cache'][0]}, {hwm['leak_into_cache'][1]} bytes")
        if name == "leak":
            for t in (0, 1):
                tmp = by_function(reader.get_temporary_allocation_records(threshold=t), ("leak_into_cache",))
                print(f"    threshold={t}: {tmp['leak_into_cache'][0]} temporary, {tmp['leak_into_cache'][1]} bytes")
            check(leaked["leak_into_cache"][0] == 8, "eight blocks stay in the module-level cache")
        else:
            try:
                list(reader.get_temporary_allocation_records(threshold=1))
                print("    temporary allocations: available")
                check(False, "an aggregated capture cannot answer the temporary question")
            except NotImplementedError as e:
                print(f"    temporary allocations: NotImplementedError: {e}")


def section_dlsym(d):
    print("== memray: an allocation that bypasses the patched stubs ==")
    reader = FileReader(d / "dlsym.bin")
    leaked = by_function(reader.get_leaked_allocation_records(),
                         ("malloc_through_dlsym", "bytearray_same_size"))
    for f, (n, size) in leaked.items():
        print(f"  {f}: {n} leaked allocation(s) recorded, {size} bytes")
    check(leaked["malloc_through_dlsym"][0] == 0, "malloc through a dlsym pointer is invisible")
    check(leaked["bytearray_same_size"][0] == 1, "the same size through CPython's allocator is seen")


def section_header(d):
    print("== memray: capture file headers ==")
    # 7 bytes: an lz4 frame descriptor ends there, and the 8th byte starts the first block's
    # size, which varies because the capture embeds the PID and timestamps.
    for label, name in (("default (lz4 on exit)", "leak"), ("--no-compress", "leak_raw")):
        head = (d / f"{name}.bin").read_bytes()[:7]
        print(f"  {label}: first 7 bytes {head.hex(' ')}")
    raw = (d / "leak_raw.bin").read_bytes()
    check(raw[:7] == b"memray\x00", "uncompressed captures start with the memray magic")
    check((d / "leak.bin").read_bytes()[:4] == bytes.fromhex("04224d18"), "compressed captures are lz4 frames")
    print(f"  --no-compress magic {raw[:6].decode()!r}, header version byte {raw[7]}")


def main():
    d = Path(sys.argv[1])
    section_small(d)
    section_churn(d)
    section_peak(d)
    section_leak(d)
    section_dlsym(d)
    section_header(d)


if __name__ == "__main__":
    main()
