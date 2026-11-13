"""A user-space stand-in for the kernel's uprobe machinery, driven through gdb's Python API.

Run by run.sh as: gdb -q -nx -batch -x probe_tracer.py ./order_server

gdb reads the same .note.stapsdt records as bpftrace and does the same three things a kernel
uprobe does for a USDT probe, with ptrace instead of a breakpoint exception handler:

  1. overwrite the nop at the probe site with a breakpoint instruction,
  2. increment the probe's semaphore so the guarded code path starts running,
  3. on each hit, fetch the arguments from the registers and memory the note names.

Environment:
  USDT_TRACE   comma-separated probe names of provider "orders" to attach to
  USDT_SITES   "provider:name pc sema" lines from stapsdt.py --sites
  USDT_REPORT  file that receives the report (gdb's own chatter stays out of it)
  USDT_TARGET_OUT  file that receives the traced program's stdout
"""

import os
import struct

import gdb

EM_AARCH64_NOP = 0xD503201F
PAGE_MASK = 0xFFF

TRACE = [name for name in os.environ["USDT_TRACE"].split(",") if name]
SITES = {}
for line in os.environ["USDT_SITES"].splitlines():
    probe, pc, sema = line.split()
    SITES[probe] = (int(pc, 16), int(sema, 16))
REPORT = open(os.environ["USDT_REPORT"], "w")


def emit(text):
    REPORT.write(text + "\n")
    REPORT.flush()


def raw_instruction(pid, pc):
    """Bytes at pc as the CPU would fetch them, read behind gdb's back through /proc/PID/mem."""
    with open(f"/proc/{pid}/mem", "rb") as mem:
        mem.seek(pc)
        return mem.read(4)


def classify(raw, arch):
    if arch.startswith("aarch64"):
        insn = struct.unpack("<I", raw)[0]
        if insn == EM_AARCH64_NOP:
            return "nop"
        if insn & 0xFFE0001F == 0xD4200000:
            return "brk (breakpoint instruction)"
        return f"unexpected instruction {insn:#010x}"
    if raw[0] == 0x90:
        return "nop"
    if raw[0] == 0xCC:
        return "int3 (breakpoint instruction)"
    return f"unexpected byte {raw[0]:#04x}"


class UsdtBreakpoint(gdb.Breakpoint):
    def __init__(self, name):
        super().__init__(f"-probe-stap orders:{name}", internal=True)
        self.probe = f"orders:{name}"
        self.hits = 0
        self.checked_site = False

    def stop(self):
        self.hits += 1
        inferior = gdb.selected_inferior()
        frame = gdb.selected_frame()
        pc = frame.pc()
        argc = int(gdb.parse_and_eval("$_probe_argc"))
        args = [int(gdb.parse_and_eval(f"$_probe_arg{i}")) for i in range(argc)]
        shown = [str(a) for a in args]
        if self.probe == "orders:order_done":
            symbol = gdb.parse_and_eval("(const char *)$_probe_arg2").string()
            shown[2] = f'"{symbol}"'
        emit(f"  hit {self.probe} argc={argc} " + " ".join(f"arg{i}={v}" for i, v in enumerate(shown)))
        if not self.checked_site:
            self.checked_site = True
            arch = frame.architecture().name()
            note_pc, sema = SITES[self.probe]
            same_page_offset = (pc & PAGE_MASK) == (note_pc & PAGE_MASK)
            live = classify(raw_instruction(inferior.pid, pc), arch)
            shadow = classify(bytes(inferior.read_memory(pc, 4)), arch)
            emit(f"    stop address matches the note location modulo the load base: "
                 f"{'yes' if same_page_offset else 'NO'}")
            emit(f"    instruction at the site in the live process: {live}")
            emit(f"    instruction gdb reports for the same address: {shadow} (the saved original)")
            if sema:
                value = int(gdb.parse_and_eval("orders_order_done_semaphore"))
                emit(f"    semaphore in the live process: {value}")
        return False  # never hand control to a human: record and continue, like a BPF program


for setting in ("pagination off", "confirm off", "debuginfod enabled off",
                "disable-randomization off", "breakpoint always-inserted on",
                "print inferior-events off"):
    gdb.execute(f"set {setting}")

breakpoints = [UsdtBreakpoint(name) for name in TRACE]
gdb.execute(f"run > {os.environ['USDT_TARGET_OUT']}")
for bp in breakpoints:
    emit(f"  {bp.probe}: {bp.hits} hits")
emit("  tracer finished")
REPORT.close()
