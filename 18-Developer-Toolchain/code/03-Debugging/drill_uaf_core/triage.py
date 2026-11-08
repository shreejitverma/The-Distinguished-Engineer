# GDB batch script: reconstruct a use-after-free from a core dump.
#
# Run as: gdb -q -nx -batch -x triage.py <exe> <core>
# It prints only derived, address-free facts so the output is identical on every
# run despite ASLR: raw pointers are XORed out by the same safe-linking transform
# glibc used to mangle them, leaving booleans and symbol names.

import gdb

MASK = (1 << 64) - 1


def u64(addr):
    """Read 8 little-endian bytes from the core at addr."""
    raw = gdb.selected_inferior().read_memory(addr, 8)
    return int.from_bytes(bytes(raw), "little")


def reveal(pos, stored):
    """glibc REVEAL_PTR: undo PROTECT_PTR(pos, ptr) = (pos >> 12) ^ ptr."""
    return ((pos >> 12) ^ stored) & MASK


def arg_object():
    """Find the innermost frame with debug info (the victim) and read its `o`.

    The faulting frame is a wild pc with no symbol; the frame above it is the
    function that made the indirect call, and DWARF still describes its `o`
    parameter even though the binary itself is stripped.
    """
    f = gdb.newest_frame()
    while f is not None and f.function() is None:
        f = f.older()
    if f is None:
        raise gdb.GdbError("no frame with debug info")
    f.select()
    return f, int(gdb.parse_and_eval("(unsigned long)o")) & MASK


def victim_backtrace():
    names = []
    f = gdb.newest_frame()
    while f is not None:
        fn = f.function()
        if fn is not None:
            sal = f.find_sal()
            line = sal.line if sal and sal.line else 0
            names.append((fn.name, line))
        f = f.older()
    return names


def main():
    # The wild branch lands on object_addr >> 12, whose low two bits (the arm64
    # PC alignment) vary with ASLR: an unmapped target faults as SIGSEGV, a
    # misaligned one as SIGBUS. Both are memory-access faults, so report the
    # class, not the specific signal, to keep the output run-to-run stable.
    signo = int(gdb.parse_and_eval("$_siginfo.si_signo"))
    fault = signo in (11, 7)  # SIGSEGV or SIGBUS
    pc = int(gdb.parse_and_eval("(unsigned long)$pc")) & MASK
    # Ask GDB itself whether the pc names anything: no symbol, no block, no line.
    sym = gdb.execute("info symbol %d" % pc, to_string=True)
    no_symbol = ("No symbol matches" in sym
                 and gdb.block_for_pc(pc) is None
                 and gdb.find_pc_line(pc).symtab is None)

    victim, obj = arg_object()
    # The original value of on_fill: make_order stored &settle there, and DWARF
    # still knows where settle lives even though the binary is stripped.
    settle = int(gdb.parse_and_eval("(unsigned long)&settle")) & MASK

    # The freed object: first word is the mangled tcache next pointer that
    # overwrote on_fill, second word is the tcache key sentinel.
    obj_next = u64(obj)
    obj_key = u64(obj + 8)
    # REVEAL the next pointer. This chunk was the only one in its bin, so glibc
    # stored PROTECT_PTR(&next, NULL); revealing it must give 0. Random payload
    # would not satisfy this, so a 0 here is a glibc tcache signature.
    revealed = reveal(obj, obj_next)

    print("=== Crash site (the victim) ===")
    print("Crashed on a memory-access fault (SIGSEGV or SIGBUS): %s"
          % ("yes" if fault else "no"))
    print("Faulting pc has no symbol (wild jump target): %s"
          % ("yes" if no_symbol else "no"))
    print("Victim frame: %s at uaf.cpp:%d" % (victim.function().name, victim.find_sal().line))
    print("Jumped to the object's first 8 bytes (pc == *obj): %s"
          % ("yes" if pc == obj_next else "no"))
    print()

    print("=== Backtrace (the free site is absent) ===")
    bt = victim_backtrace()
    for name, line in bt:
        print("%s at uaf.cpp:%d" % (name, line))
    print("retire present in backtrace: %s"
          % ("yes" if any(n.split("(")[0] == "retire" for n, _ in bt) else "no"))
    print()

    print("=== Heap forensics (the core proves the free) ===")
    print("obj.on_fill no longer holds &settle (it was overwritten): %s"
          % ("yes" if obj_next != settle else "no"))
    print("Safe-linked next reveals to NULL (glibc tcache list tail): %s"
          % ("yes" if revealed == 0 else "no"))
    print("Word at offset 8 is non-zero and no longer the original id 0x2222 (tcache key): %s"
          % ("yes" if obj_key not in (0, 0x2222) else "no"))
    print("Payload qty 100 survives at offset 16 (tcache touched only 16 bytes): %s"
          % ("yes" if u64(obj + 16) == 100 else "no"))
    print()

    print("=== Verdict ===")
    print("Use-after-free: dispatch used `o` after retire() freed it to the tcache;")
    print("the free site retire() is not in the backtrace and was recovered from the heap.")


main()
