#!/usr/bin/env python3
"""Inspect ELF symbol tables of the four lab binaries and print what each pipeline changed."""

import subprocess
import sys


def get_symbols(binary_path):
    """Map symbol name to (size or None, nm kind) from `nm -S`; clone suffixes such as .isra.0 are kept in the name."""
    res = subprocess.run(["nm", "-S", binary_path], capture_output=True, text=True, check=True)
    syms = {}
    for line in res.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) == 4:
            syms[parts[3]] = (int(parts[1], 16), parts[2])
        elif len(parts) == 3:
            syms[parts[2]] = (None, parts[1])
        elif len(parts) == 2:
            syms[parts[1]] = (None, parts[0])
    return syms


def lookup(syms, name):
    """Return (size, kind) for name or a compiler clone name.N, or (None, None) when the symbol is absent."""
    for sym, value in syms.items():
        if sym == name or sym.startswith(name + "."):
            return value
    return None, None


def linkage(syms, name):
    kind = lookup(syms, name)[1]
    return {"T": "GLOBAL", "t": "LOCAL", None: "MISSING"}.get(kind, "OTHER")


def presence(syms, name):
    return "PRESENT" if lookup(syms, name)[1] else "STRIPPED"


def inlined(syms, name):
    """INLINED only when no symbol remains; a surviving local copy is reported as LOCAL_COPY."""
    state = linkage(syms, name)
    return {"MISSING": "INLINED", "LOCAL": "LOCAL_COPY"}.get(state, "NOT_INLINED")


def main():
    if len(sys.argv) != 5:
        print("Usage: analyze_layout.py <app_base> <app_base_gc> <app_lto> <app_pgo>")
        sys.exit(1)

    base, base_gc, lto, pgo = (get_symbols(path) for path in sys.argv[1:])

    print("=== Symbol Resolution & Cross-TU Optimization ===")
    print(f"packet_process in baseline: {linkage(base, 'packet_process')}")
    print(f"packet_process in LTO: {inlined(lto, 'packet_process')}")
    print(f"packet_process in PGO: {inlined(pgo, 'packet_process')}")

    print(f"dead_debug_helper in baseline: {presence(base, 'dead_debug_helper')}")
    print(f"dead_debug_helper in baseline with --gc-sections: {presence(base_gc, 'dead_debug_helper')}")
    print(f"dead_debug_helper in LTO: {presence(lto, 'dead_debug_helper')}")
    print(f"dead_debug_helper in PGO: {presence(pgo, 'dead_debug_helper')}")

    print(
        "handle_cold_error linkage: "
        f"base={linkage(base, 'handle_cold_error')}, "
        f"lto={linkage(lto, 'handle_cold_error')}, "
        f"pgo={linkage(pgo, 'handle_cold_error')}"
    )

    print("=== Profile Effect on Code Generation ===")
    lto_size = lookup(lto, "main")[0]
    pgo_size = lookup(pgo, "main")[0]
    print(f"main size in LTO: {lto_size} bytes")
    print(f"main size in PGO: {pgo_size} bytes")
    changed = lto_size is not None and pgo_size is not None and lto_size != pgo_size
    print(f"PGO changed main: {'YES' if changed else 'NO'}")


if __name__ == "__main__":
    main()
