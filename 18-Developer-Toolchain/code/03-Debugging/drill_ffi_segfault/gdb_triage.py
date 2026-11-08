"""GDB batch script: run the driver to its fault and print address-free facts.

Invoked by run.sh as:
    gdb -nx -batch -x gdb_triage.py --args python3 driver.py LIB large ADDR_FILE
with DRILL_ADDR_FILE in the environment. Every fact line starts with "TRIAGE|"
so run.sh can drop GDB's own chatter. The last line is "TRIAGE|done".
"""

import os
import re

import gdb  # provided by GDB's embedded Python

SIGNALS = {11: "SIGSEGV", 7: "SIGBUS"}  # Linux numbers on x86-64 and arm64
SEGV_CODES = {1: "SEGV_MAPERR", 2: "SEGV_ACCERR"}
MAPPING_RE = re.compile(r"^\s*(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s", re.MULTILINE)
PY_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')


def out(text):
    print(f"TRIAGE|{text}", flush=True)


def yn(flag):
    return "yes" if flag else "no"


def mapped(addr):
    text = gdb.execute("info proc mappings", to_string=True)
    return any(int(lo, 16) <= addr < int(hi, 16) for lo, hi in MAPPING_RE.findall(text))


def frames(limit=256):
    found = []
    frame = gdb.newest_frame()
    while frame is not None and len(found) < limit:
        found.append(frame)
        try:
            frame = frame.older()
        except gdb.error:
            break
    return found


def solib(frame):
    return gdb.solib_name(frame.pc()) or "<main executable>"


def location(frame):
    sal = frame.find_sal()
    if sal.symtab is None:
        return "no line info"
    return f"{os.path.basename(sal.symtab.filename)}:{sal.line}"


def main():
    gdb.execute("set pagination off")
    gdb.execute("set confirm off")
    # Silence the inferior so its output cannot interleave with ours. `run ARGS`
    # replaces the --args list, so the redirection must repeat the arguments.
    args = gdb.parameter("args")
    gdb.execute(f"run {args} > /dev/null 2>&1", to_string=True)

    # Frame 0 is Rust; parse siginfo expressions as C, whose syntax they are written in.
    gdb.execute("set language c")
    try:
        signo = int(gdb.parse_and_eval("$_siginfo.si_signo"))
    except gdb.error:
        out("stopped on a signal: no (the stale read did not fault)")
        out("done")
        return
    code = int(gdb.parse_and_eval("$_siginfo.si_code"))
    addr = int(gdb.parse_and_eval("$_siginfo._sifields._sigfault.si_addr"))
    out(f"signal: {SIGNALS.get(signo, signo)} ({SEGV_CODES.get(code, code)})")

    with open(os.environ["DRILL_ADDR_FILE"]) as fh:
        ptr, length = (int(field) for field in fh.read().split())
    out(
        f"fault address inside the buffer Python freed: {yn(ptr <= addr < ptr + length)}"
    )
    out(f"fault address offset into that buffer: {addr - ptr}")
    out(f"fault address inside any current mapping: {yn(mapped(addr))}")

    stack = frames()
    out(f"frame 0 is in libticktape: {yn('libticktape' in solib(stack[0]))}")
    ours = [
        f
        for f in stack
        if "libticktape" in solib(f) and location(f).startswith("lib.rs:")
    ]
    if ours:
        out(f"innermost ticktape source frame: {ours[0].name()} at {location(ours[0])}")
    else:
        out("innermost ticktape source frame: none (no DWARF for libticktape?)")
    first_ffi = next((i for i, f in enumerate(stack) if "libffi" in solib(f)), None)
    last_rust = max(
        (i for i, f in enumerate(stack) if "libticktape" in solib(f)), default=None
    )
    out(
        f"libffi frame directly below the Rust frames: {yn(first_ffi is not None and last_rust is not None and first_ffi == last_rust + 1)}"
    )
    # The inferior is python3 itself, so main-executable frames are interpreter frames.
    below = stack[first_ffi + 1 :] if first_ffi is not None else []
    interp = [
        f
        for f in below
        if solib(f) == "<main executable>" or "python" in os.path.basename(solib(f))
    ]
    out(f"interpreter frames below libffi: {yn(bool(interp))}")

    try:
        text = gdb.execute("py-bt", to_string=True)
    except gdb.error:
        out("py-bt: unavailable (no python-gdb.py auto-loaded for this interpreter)")
    else:
        hits = PY_FRAME_RE.findall(text)
        if hits:
            path, line, func = hits[0]
            out(
                f"py-bt innermost Python frame: {os.path.basename(path)}:{line} in {func}"
            )
        else:
            out(
                "py-bt: loaded, but found no Python frame (interpreter built without frame info?)"
            )
    out("done")


main()
