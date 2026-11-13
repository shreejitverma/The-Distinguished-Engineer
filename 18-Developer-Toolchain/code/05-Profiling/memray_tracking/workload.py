"""Workloads that memray runs; each section isolates one allocation pattern.

Run as `python -m memray run -o out.bin workload.py <section>`; analyze.py reads the capture.
Every function name here is unique so the analyzer can group records by the innermost Python frame.
"""

import ctypes
import ctypes.util
import sys

N_SMALL = 50_000
N_APPEND = 1_000_000
BIG = 10_000_000
LEAK_BLOCK = 100_000
LEAK_COUNT = 8
CTYPES_BLOCK = 7_777_777

_cache = []


def make_small_tuples():
    # 50,000 three-element tuples, each well under pymalloc's 512-byte limit, all kept alive.
    return [(i, i, i) for i in range(N_SMALL)]


def grow_by_append():
    out = []
    for _ in range(N_APPEND):
        out.append(None)
    return out


def preallocate():
    return [None] * N_APPEND


def peak_sequential():
    # Three 10 MB buffers, each freed before the next is made: the heap never holds two.
    for _ in range(3):
        buf = bytearray(BIG)
        del buf


def peak_together():
    # The same three buffers held at once: the high-water mark is three times larger.
    held = [bytearray(BIG) for _ in range(3)]
    del held


def leak_into_cache():
    # Each bytes object is above 512 bytes, so it comes from malloc, never from pymalloc.
    for i in range(LEAK_COUNT):
        _cache.append(bytes(LEAK_BLOCK) + bytes([i]))


def malloc_through_dlsym():
    # ctypes resolves malloc with dlsym and calls the returned address directly,
    # so the call never passes through an import stub that memray patched.
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    libc.malloc.restype = ctypes.c_void_p
    libc.malloc.argtypes = [ctypes.c_size_t]
    libc.free.argtypes = [ctypes.c_void_p]
    ptr = libc.malloc(CTYPES_BLOCK)
    if not ptr:
        raise MemoryError("malloc returned NULL")
    _cache.append(ptr)


def bytearray_same_size():
    _cache.append(bytearray(CTYPES_BLOCK))


SECTIONS = {
    "small": [make_small_tuples],
    "churn": [grow_by_append, preallocate],
    "peak": [peak_sequential, peak_together],
    "leak": [leak_into_cache],
    "dlsym": [malloc_through_dlsym, bytearray_same_size],
}

if __name__ == "__main__":
    keep = []
    for fn in SECTIONS[sys.argv[1]]:
        keep.append(fn())
