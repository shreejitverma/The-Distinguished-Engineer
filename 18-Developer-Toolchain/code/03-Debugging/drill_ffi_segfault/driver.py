"""Drive libticktape through ctypes and print address-free facts.

Usage: python3 driver.py LIBRARY MODE [ADDR_FILE]

Modes:
  small  feed a 256-byte bytes object (a pymalloc object), drop it, allocate a
         same-size replacement, then read through the stale pointer.
  large  feed a 1 MiB bytes object (malloc, above glibc's initial mmap
         threshold), drop it, then read through the stale pointer.
  warm   free one 1 MiB bytes object first, then run the large scenario.
  fixed  run the small and large scenarios through tape_feed_copy.
  fault  feed the unmapped address 16 and read it: a fault on every platform,
         independent of any allocator, so the crash tooling can be checked everywhere.

ADDR_FILE, when given, receives "PTR LEN" for the stale buffer just before the
read, so a debugger script can compare the fault address with it.
"""

from __future__ import annotations

import ctypes
import sys

SMALL = 256  # plus the 33-byte bytes header: 289 bytes, a pymalloc request (<= 512)
LARGE = 1 << 20  # served by malloc; above glibc's initial 128 KiB mmap threshold
UNMAPPED = 16  # inside the null guard region on Linux and macOS: never mapped
FILL_OLD = 0x41
FILL_NEW = 0x42


def load(path: str) -> ctypes.CDLL:
    lib = ctypes.CDLL(path)
    lib.tape_new.argtypes = ()
    lib.tape_new.restype = ctypes.c_void_p
    for name in ("tape_feed", "tape_feed_copy"):
        fn = getattr(lib, name)
        fn.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t)
        fn.restype = None
    lib.tape_sum.argtypes = (ctypes.c_void_p,)
    lib.tape_sum.restype = ctypes.c_uint64
    lib.tape_data_ptr.argtypes = (ctypes.c_void_p,)
    lib.tape_data_ptr.restype = ctypes.c_size_t
    lib.tape_free.argtypes = (ctypes.c_void_p,)
    lib.tape_free.restype = None
    return lib


def make(fill: int, n: int) -> bytes:
    # Built at run time: a literal such as b"A" * 256 could be folded into a
    # code-object constant, which lives as long as the code and is never freed.
    return bytes([fill]) * n


def yn(flag: bool) -> str:
    return "yes" if flag else "no"


def feed_then_drop(feed, tape: int, n: int) -> int:
    """Feed a fresh bytes object, drop the only Python reference, return its id()."""
    payload = make(FILL_OLD, n)
    feed(tape, payload, n)
    old_id = id(payload)
    del payload  # refcount hits 0: CPython frees the object right here
    return old_id


def say(text: str) -> None:
    print(text, flush=True)


def stale_read(
    lib: ctypes.CDLL, tape: int, n: int, addr_file: str | None, tag: str
) -> None:
    if addr_file:
        with open(addr_file, "w") as fh:
            fh.write(f"{lib.tape_data_ptr(tape)} {n}\n")
    say(f"{tag}: reading {n} bytes through the stale pointer")
    total = lib.tape_sum(tape)
    say(f"{tag}: tape_sum returned (no crash)")
    say(f"{tag}: sum equals the freed payload's sum: {yn(total == FILL_OLD * n)}")


def run_small(lib: ctypes.CDLL, addr_file: str | None) -> None:
    tape = lib.tape_new()
    old_id = feed_then_drop(lib.tape_feed, tape, SMALL)
    replacement = make(FILL_NEW, SMALL)
    say(
        f"small: replacement object reuses the freed block: {yn(id(replacement) == old_id)}"
    )
    if addr_file:
        with open(addr_file, "w") as fh:
            fh.write(f"{lib.tape_data_ptr(tape)} {SMALL}\n")
    total = lib.tape_sum(tape)
    say("small: tape_sum returned (no crash)")
    say(f"small: sum equals the freed payload's sum: {yn(total == FILL_OLD * SMALL)}")
    say(f"small: sum equals the replacement's sum: {yn(total == FILL_NEW * SMALL)}")
    del replacement
    lib.tape_free(tape)


def run_large(lib: ctypes.CDLL, addr_file: str | None, tag: str = "large") -> None:
    tape = lib.tape_new()
    feed_then_drop(lib.tape_feed, tape, LARGE)
    stale_read(lib, tape, LARGE, addr_file, tag)
    lib.tape_free(tape)


def run_warm(lib: ctypes.CDLL, addr_file: str | None) -> None:
    warmup = make(0x57, LARGE)
    del warmup  # first free of an mmapped chunk: glibc raises its mmap threshold
    run_large(lib, addr_file, tag="warm")


def run_fixed(lib: ctypes.CDLL) -> None:
    for n, tag in ((SMALL, "fixed-small"), (LARGE, "fixed-large")):
        tape = lib.tape_new()
        feed_then_drop(lib.tape_feed_copy, tape, n)
        replacement = make(FILL_NEW, n)
        total = lib.tape_sum(tape)
        say(
            f"{tag}: sum equals the original payload's sum: {yn(total == FILL_OLD * n)}"
        )
        del replacement
        lib.tape_free(tape)


def run_fault(lib: ctypes.CDLL) -> None:
    # tape_feed declares its data argument as c_char_p; redeclare it as a plain
    # address for this call so an integer can be passed.
    feed_addr = ctypes.CFUNCTYPE(
        None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t
    )(("tape_feed", lib))
    tape = lib.tape_new()
    feed_addr(tape, UNMAPPED, 1)
    stale_read(lib, tape, 1, None, "fault")
    lib.tape_free(tape)


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        return 2
    lib = load(argv[1])
    mode = argv[2]
    addr_file = argv[3] if len(argv) == 4 else None
    if mode == "small":
        run_small(lib, addr_file)
    elif mode == "large":
        run_large(lib, addr_file)
    elif mode == "warm":
        run_warm(lib, addr_file)
    elif mode == "fixed":
        run_fixed(lib)
    elif mode == "fault":
        run_fault(lib)
    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
