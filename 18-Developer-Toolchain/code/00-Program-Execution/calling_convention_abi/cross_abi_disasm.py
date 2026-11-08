#!/usr/bin/env python3
"""Cross-target calling convention inspector: System V AMD64 vs AAPCS64.

Compiles targeted C snippets for both x86_64 and aarch64 ELF targets using
LLVM's multi-target backend and disassembles them to mechanically verify ABI
rules: register assignment, red zone, HFA mapping, indirect return, and %al.
"""

import os
import re
import subprocess
import sys
import tempfile

SNIPPET_SOURCE = r"""
#include <stdint.h>

typedef struct {
    uint64_t a;
    uint64_t b;
} Pair16;

typedef struct {
    uint64_t a;
    uint64_t b;
    uint64_t c;
} Triple24;

typedef struct {
    float x;
    float y;
    float z;
    float w;
} FloatHFA;

/* 1. Argument passing across register boundaries */
uint64_t sum_9_args(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4,
                    uint64_t a5, uint64_t a6, uint64_t a7, uint64_t a8,
                    uint64_t a9) {
    return a1 + a2 + a3 + a4 + a5 + a6 + a7 + a8 + a9;
}

/* 2. Leaf function stack usage: Red Zone vs SP adjustment */
uint64_t leaf_stack_buffer(uint64_t val) {
    volatile uint64_t buf[2];
    buf[0] = val;
    buf[1] = val + 42;
    return buf[0] + buf[1];
}

/* 3. Homogeneous Floating-point Aggregate */
float sum_hfa(FloatHFA v) {
    return v.x + v.y + v.z + v.w;
}

/* 4. Composite returns */
Pair16 return_pair(uint64_t x, uint64_t y) {
    Pair16 p = {x, y};
    return p;
}

Triple24 return_triple(uint64_t x, uint64_t y, uint64_t z) {
    Triple24 t = {x, y, z};
    return t;
}

/* 5. Variadic calls */
int variadic_sink(int count, ...);

int call_var_int(int a) {
    return variadic_sink(1, a);
}

int call_var_float(int a, double d) {
    return variadic_sink(2, a, d);
}
"""

def compile_and_disasm(cc: str, target: str, src_path: str, tmpdir: str) -> str:
    obj_path = os.path.join(tmpdir, f"{target}.o")
    cmd_cc = [cc, "-target", target, "-O2", "-c", src_path, "-o", obj_path]
    subprocess.run(cmd_cc, check=True, capture_output=True)
    objdump = os.environ.get("OBJDUMP", "objdump")
    res = subprocess.run([objdump, "-d", obj_path], check=True, capture_output=True, text=True)
    return res.stdout


# "   10: 48 03 44 24 08      <TAB>addq<TAB>0x8(%rsp), %rax  # comment"
INSN_RE = re.compile(r"^\s*[0-9a-f]+:\s+(?:[0-9a-f]{2,8}\s+)+\t(\S+)(?:\t(.*?))?\s*$")


def instructions(disasm: str, func_name: str) -> list[str]:
    """Return the function's instructions as 'mnemonic operands' with trailing comments removed."""
    out: list[str] = []
    recording = False
    for line in disasm.splitlines():
        m = re.match(r"^[0-9a-fA-F]+ <([^>]+)>:", line)
        if m:
            if recording:
                break
            recording = m.group(1) == func_name
            continue
        if recording:
            im = INSN_RE.match(line)
            if im:
                ops = re.split(r"\s{2,}#|\s+//", im.group(2) or "")[0].strip()
                out.append(f"{im.group(1)} {ops}".strip())
    if not out:
        fail(f"function {func_name} not found in disassembly")
    return out


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def has(insns: list[str], pattern: str) -> bool:
    return any(re.fullmatch(pattern, i) for i in insns)


def require_format(disasm: str, fmt: str) -> None:
    if f"file format {fmt}" not in disasm:
        fail(f"objdump output is not {fmt}")


def main():
    cc = os.environ.get("CC", "cc")
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "snippets.c")
        with open(src_path, "w") as f:
            f.write(SNIPPET_SOURCE)

        disasm_x86 = compile_and_disasm(cc, "x86_64-unknown-linux-gnu", src_path, tmpdir)
        disasm_arm = compile_and_disasm(cc, "aarch64-unknown-linux-gnu", src_path, tmpdir)
        require_format(disasm_x86, "elf64-x86-64")
        require_format(disasm_arm, "elf64-littleaarch64")

        def x86(name: str) -> list[str]:
            return instructions(disasm_x86, name)

        def arm(name: str) -> list[str]:
            return instructions(disasm_arm, name)

        print("=== Cross-Target ABI Inspection (System V AMD64 vs AAPCS64) ===")

        # Feature 1: GPR arguments. The first operand of each add/lea chain must read exactly the
        # ABI registers, and the stack arguments must sit at the offsets above the return address.
        sum_x86 = x86("sum_9_args")
        read_x86 = " ".join(sum_x86)
        for reg in ("%rdi", "%rsi", "%rdx", "%rcx", "%r8", "%r9"):
            if not re.search(rf"{re.escape(reg)}(?![0-9a-z])", read_x86):
                fail(f"x86-64 sum_9_args never reads {reg}")
        for off in ("0x8", "0x10", "0x18"):
            if not has(sum_x86, rf"addq {off}\(%rsp\), %rax"):
                fail(f"x86-64 sum_9_args does not read stack argument at {off}(%rsp)")
        sum_arm = arm("sum_9_args")
        read_arm = " ".join(sum_arm)
        for i in range(8):
            if not re.search(rf"\bx{i}\b", read_arm):
                fail(f"AArch64 sum_9_args never reads x{i}")
        if not has(sum_arm, r"ldr x\d+, \[sp\]"):
            fail("AArch64 sum_9_args does not load argument 9 from [sp]")
        if sum(1 for i in sum_arm if re.search(r"\[sp", i)) != 1:
            fail("AArch64 sum_9_args touches the stack more than once")
        print("[PASS] GPR Argument Allocation:")
        print("       x86-64 System V : 6 GPRs (%rdi, %rsi, %rdx, %rcx, %r8, %r9); args 7-9 read from stack")
        print("       AAPCS64 AArch64 : 8 GPRs (x0-x7); arg 9 read from stack")

        # Feature 2: Red Zone. x86-64 must store below %rsp without ever moving it; AArch64 must
        # allocate and release the frame.
        rz_x86 = x86("leaf_stack_buffer")
        below = [int(m.group(1), 16) for i in rz_x86 for m in re.finditer(r"-(0x[0-9a-f]+)\(%rsp\)", i)]
        moves_rsp = [i for i in rz_x86 if re.search(r"%rsp\s*$", i) and re.match(r"(sub|add|lea|mov|and)", i)]
        if not below or max(below) > 128 or moves_rsp or any(i.startswith(("push", "pop")) for i in rz_x86):
            fail("x86-64 leaf_stack_buffer does not use the red zone without moving %rsp")
        rz_arm = arm("leaf_stack_buffer")
        allocs = [i for i in rz_arm if re.fullmatch(r"sub sp, sp, #0x[0-9a-f]+", i)]
        frees = [i for i in rz_arm if re.fullmatch(r"add sp, sp, #0x[0-9a-f]+", i) or re.search(r"\[sp\], #0x[0-9a-f]+$", i)]
        if len(allocs) != 1 or len(frees) != 1:
            fail("AArch64 leaf_stack_buffer does not allocate and release its frame by moving sp")
        print("[PASS] Leaf Function Red Zone:")
        print("       x86-64 System V : 128-byte Red Zone utilized; %rsp unchanged (no subq %rsp)")
        print("       AAPCS64 AArch64 : No standard Red Zone; sp decremented (sub sp, sp, #imm)")

        # Feature 3: HFA vs vector packing.
        hfa_x86 = x86("sum_hfa")
        shuffles = [i for i in hfa_x86 if i.startswith("shufps")]
        adds = [i for i in hfa_x86 if i.startswith("addss")]
        if len(shuffles) < 2 or len(adds) != 3 or not any(re.search(r"%xmm1,", i) for i in adds):
            fail("x86-64 sum_hfa does not unpack two packed xmm registers with shufps")
        hfa_arm = arm("sum_hfa")
        body = hfa_arm[:-1]
        if hfa_arm[-1] != "ret" or len(body) != 3 or not all(i.startswith("fadd ") for i in body):
            fail("AArch64 sum_hfa is not three fadd instructions and a ret")
        for i in range(4):
            if not any(re.search(rf"\bs{i}\b", ins) for ins in body):
                fail(f"AArch64 sum_hfa never reads s{i}")
        print("[PASS] Homogeneous Floating-point Aggregates (HFA):")
        print("       x86-64 System V : Struct packed into two SSE eightbytes (%xmm0, %xmm1); requires shufps")
        print("       AAPCS64 AArch64 : Direct binding to scalar registers s0-s3; zero unpack overhead")

        # Feature 4: composite returns.
        pair_x86 = x86("return_pair")
        if not (has(pair_x86, r"movq %rdi, %rax") and has(pair_x86, r"movq %rsi, %rdx")):
            fail("x86-64 return_pair does not return its fields in %rax:%rdx")
        if arm("return_pair") != ["ret"]:
            fail("AArch64 return_pair is not a bare ret (x0:x1 pass straight through)")
        trip_x86 = x86("return_triple")
        if not (has(trip_x86, r"movq %rdi, %rax")
                and has(trip_x86, r"movq %rsi, \(%rdi\)")
                and has(trip_x86, r"movq %rdx, 0x8\(%rdi\)")
                and has(trip_x86, r"movq %rcx, 0x10\(%rdi\)")):
            fail("x86-64 return_triple does not take its buffer in %rdi and shift the arguments to %rsi, %rdx, %rcx")
        trip_arm = arm("return_triple")
        if not (has(trip_arm, r"stp x0, x1, \[x8\]") and has(trip_arm, r"str x2, \[x8, #0x10\]")):
            fail("AArch64 return_triple does not write x0-x2 through the buffer in x8")
        print("[PASS] Composite Return Value Classification:")
        print("       <=16B Struct    : x86-64 uses %rax:%rdx; AAPCS64 uses x0:x1")
        print("       >16B Struct     : x86-64 passes buffer in %rdi; AAPCS64 passes buffer in dedicated x8")

        # Feature 5: variadic vector count (%al).
        var_int = x86("call_var_int")
        var_flt = x86("call_var_float")
        if not (has(var_int, r"xorl %eax, %eax") and not has(var_int, r"movb .*, %al")):
            fail("x86-64 call_var_int does not zero %al with xorl %eax, %eax")
        if not (has(var_flt, r"movb \$0x1, %al") and not has(var_flt, r"xorl %eax, %eax")):
            fail("x86-64 call_var_float does not set %al to 1")
        if not (has(arm("call_var_int"), r"mov w1, w0") and has(arm("call_var_float"), r"mov w1, w0")):
            fail("AArch64 variadic calls do not pass the fixed arguments in w0-w1")
        if any(re.search(r"\bw8\b|\bx8\b|\bal\b", i) for i in arm("call_var_float")):
            fail("AArch64 variadic call sets a vector count register")
        print("[PASS] Variadic Vector Register Count (%al):")
        print("       Integer varargs : %al set to 0 (xorl %eax, %eax) to skip vector register save")
        print("       FP varargs      : %al set to 1 (movb $0x1, %al) signaling one active vector register")
        print("       AAPCS64 Linux   : no count register; the count and the variadic int travel in w0-w1 and nothing like %al is set")


if __name__ == "__main__":
    main()
