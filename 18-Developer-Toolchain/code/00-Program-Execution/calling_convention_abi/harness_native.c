/*
 * Native AAPCS64 (Apple arm64) probes.
 *
 * Each probe is a callee written in assembly, so the C compiler cannot specialize it away.
 * The probe records the raw register and stack-slot contents it was handed, and the C side
 * checks them against what the calling convention says.
 * A different convention (arguments in other registers, variadics in registers, a shifted
 * indirect-return pointer) makes these checks fail.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <signal.h>
#include <setjmp.h>
#include <stdarg.h>

#if !defined(__aarch64__) || !defined(__APPLE__)
#error "harness_native.c probes the Apple arm64 calling convention; run it on an arm64 Mac"
#endif

typedef struct {
    float x;
    float y;
    float z;
    float w;
} FloatHFA;

typedef struct {
    uint64_t low;
    uint64_t high;
} SmallPair;

typedef struct {
    uint64_t a;
    uint64_t b;
    uint64_t c;
} BigTriple;

/* Scratch the probes write into; x9 and x10 are caller-saved and never carry arguments. */
uint64_t abi_probe_out[16];

extern void abi_probe_int(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t,
                          uint64_t, uint64_t, uint64_t, uint64_t);
extern void abi_probe_fp(double, double, double, double, double, double, double, double, double);
extern void abi_probe_mixed(uint64_t, double, uint64_t, double);
extern void abi_probe_hfa(FloatHFA);
extern void abi_probe_var(int, ...);
extern void abi_probe_sp(void);
extern SmallPair abi_ret_pair(void);
extern BigTriple abi_ret_triple(uint64_t, uint64_t, uint64_t);

#define LOAD_OUT \
    "  adrp x9, _abi_probe_out@PAGE\n" \
    "  add  x9, x9, _abi_probe_out@PAGEOFF\n"

__asm__(
    ".text\n"
    ".p2align 2\n"

    /* out[0..7] = x0..x7, out[8] = the stack slot at [sp]. */
    ".globl _abi_probe_int\n"
    "_abi_probe_int:\n"
    LOAD_OUT
    "  stp x0, x1, [x9]\n"
    "  stp x2, x3, [x9, #16]\n"
    "  stp x4, x5, [x9, #32]\n"
    "  stp x6, x7, [x9, #48]\n"
    "  ldr x10, [sp]\n"
    "  str x10, [x9, #64]\n"
    "  ret\n"

    /* out[0..7] = d0..d7, out[8] = the stack slot at [sp]. */
    ".globl _abi_probe_fp\n"
    "_abi_probe_fp:\n"
    LOAD_OUT
    "  stp d0, d1, [x9]\n"
    "  stp d2, d3, [x9, #16]\n"
    "  stp d4, d5, [x9, #32]\n"
    "  stp d6, d7, [x9, #48]\n"
    "  ldr x10, [sp]\n"
    "  str x10, [x9, #64]\n"
    "  ret\n"

    /* out[0] = x0, out[1] = d0, out[2] = x1, out[3] = d1. */
    ".globl _abi_probe_mixed\n"
    "_abi_probe_mixed:\n"
    LOAD_OUT
    "  str x0, [x9]\n"
    "  str d0, [x9, #8]\n"
    "  str x1, [x9, #16]\n"
    "  str d1, [x9, #24]\n"
    "  ret\n"

    /* out as float[4] = s0..s3. */
    ".globl _abi_probe_hfa\n"
    "_abi_probe_hfa:\n"
    LOAD_OUT
    "  str s0, [x9]\n"
    "  str s1, [x9, #4]\n"
    "  str s2, [x9, #8]\n"
    "  str s3, [x9, #12]\n"
    "  ret\n"

    /* out[0] = count (w0), out[1..3] = stack slots [sp], [sp+8], [sp+16]. */
    ".globl _abi_probe_var\n"
    "_abi_probe_var:\n"
    LOAD_OUT
    "  uxtw x10, w0\n"
    "  str x10, [x9]\n"
    "  ldr x10, [sp]\n"
    "  str x10, [x9, #8]\n"
    "  ldr x10, [sp, #8]\n"
    "  str x10, [x9, #16]\n"
    "  ldr x10, [sp, #16]\n"
    "  str x10, [x9, #24]\n"
    "  ret\n"

    /* out[0] = SP as seen on entry. */
    ".globl _abi_probe_sp\n"
    "_abi_probe_sp:\n"
    LOAD_OUT
    "  mov x10, sp\n"
    "  str x10, [x9]\n"
    "  ret\n"

    /* Returns {100, 200} in x0:x1 with no memory involved. */
    ".globl _abi_ret_pair\n"
    "_abi_ret_pair:\n"
    "  mov x0, #100\n"
    "  mov x1, #200\n"
    "  ret\n"

    /* The result buffer arrives in x8; x0..x2 keep the three explicit arguments. */
    ".globl _abi_ret_triple\n"
    "_abi_ret_triple:\n"
    "  stp x0, x1, [x8]\n"
    "  str x2, [x8, #16]\n"
    "  ret\n"
);

#define CHECK(cond, what) \
    do { \
        if (!(cond)) { \
            fprintf(stderr, "FAIL: %s\n", what); \
            exit(1); \
        } \
    } while (0)

static sigjmp_buf g_fault_jmp;
static volatile int g_fault_sig;
static volatile int g_fault_code;

static void fault_handler(int sig, siginfo_t *si, void *ucontext) {
    (void)ucontext;
    g_fault_sig = sig;
    g_fault_code = si->si_code;
    siglongjmp(g_fault_jmp, 1);
}

static uint64_t bits(double d) {
    uint64_t u;
    memcpy(&u, &d, sizeof u);
    return u;
}

static void test_int_args(void) {
    memset(abi_probe_out, 0xEE, sizeof abi_probe_out);
    abi_probe_int(1, 2, 3, 4, 5, 6, 7, 8, 9);
    for (int i = 0; i < 9; i++) {
        CHECK(abi_probe_out[i] == (uint64_t)(i + 1), "integer argument not in x0-x7 or [sp]");
    }
    printf("[PASS] Integer argument passing: probe saw args 1-8 in x0-x7 and arg 9 at [sp]\n");
}

static void test_fp_args(void) {
    memset(abi_probe_out, 0xEE, sizeof abi_probe_out);
    abi_probe_fp(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0);
    for (int i = 0; i < 9; i++) {
        CHECK(abi_probe_out[i] == bits((double)(i + 1)), "double argument not in d0-d7 or [sp]");
    }
    memset(abi_probe_out, 0xEE, sizeof abi_probe_out);
    abi_probe_mixed(11, 2.5, 33, 4.5);
    CHECK(abi_probe_out[0] == 11 && abi_probe_out[1] == bits(2.5) &&
          abi_probe_out[2] == 33 && abi_probe_out[3] == bits(4.5),
          "integer and floating-point arguments do not use independent register counters");
    printf("[PASS] Floating-point argument passing: probe saw args 1-8 in d0-d7 and arg 9 at [sp]; integer and FP counters are independent\n");
}

static void test_hfa(void) {
    FloatHFA hfa = {1.5f, 2.5f, 3.5f, 4.5f};
    float seen[4];
    memset(abi_probe_out, 0xEE, sizeof abi_probe_out);
    abi_probe_hfa(hfa);
    memcpy(seen, abi_probe_out, sizeof seen);
    CHECK(seen[0] == 1.5f && seen[1] == 2.5f && seen[2] == 3.5f && seen[3] == 4.5f,
          "HFA members not in s0-s3");
    printf("[PASS] Homogeneous Floating-point Aggregate (HFA): probe saw the 4 floats in s0-s3\n");
}

static void test_returns(void) {
    SmallPair pair = abi_ret_pair();
    CHECK(pair.low == 100 && pair.high == 200, "16-byte struct not returned in x0:x1");
    printf("[PASS] Small composite return (16B): a callee that only sets x0 and x1 returns the struct\n");

    BigTriple trip = abi_ret_triple(10, 20, 30);
    CHECK(trip.a == 10 && trip.b == 20 && trip.c == 30,
          "24-byte struct not written through x8 with x0-x2 unshifted");
    printf("[PASS] Large composite return (24B): a callee that writes through x8 fills the buffer and still sees its 3 arguments in x0-x2\n");
}

static void test_stack_alignment(void) {
    abi_probe_sp();
    CHECK((abi_probe_out[0] & 0xF) == 0, "SP is not 16-byte aligned at function entry");

    struct sigaction sa;
    struct sigaction old_bus;
    struct sigaction old_segv;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = fault_handler;
    sa.sa_flags = SA_SIGINFO;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGBUS, &sa, &old_bus);
    sigaction(SIGSEGV, &sa, &old_segv);

    /* Control: a store through an aligned, adjusted SP must not fault. */
    __asm__ volatile(
        "sub sp, sp, #16\n"
        "str xzr, [sp]\n"
        "add sp, sp, #16\n"
        ::: "memory");

    g_fault_sig = 0;
    g_fault_code = 0;
    if (sigsetjmp(g_fault_jmp, 1) == 0) {
        __asm__ volatile(
            "sub sp, sp, #8\n"
            "str xzr, [sp]\n"
            "add sp, sp, #8\n"
            ::: "memory");
        fprintf(stderr, "FAIL: Misaligned SP access did not trigger hardware fault\n");
        exit(1);
    }

    sigaction(SIGBUS, &old_bus, NULL);
    sigaction(SIGSEGV, &old_segv, NULL);
    CHECK(g_fault_sig == SIGBUS, "misaligned SP store did not raise SIGBUS");
    CHECK(g_fault_code == BUS_ADRALN, "misaligned SP store did not report BUS_ADRALN");
    printf("[PASS] Stack alignment invariant: SP is 16-byte aligned at callee entry; a store through a misaligned SP raises SIGBUS with BUS_ADRALN, an aligned one does not\n");
}

/* On Apple arm64 va_list is a plain pointer into the argument area. */
_Static_assert(__builtin_types_compatible_p(va_list, char *), "Apple arm64 va_list is char *");

static uint64_t sum_variadic(int count, ...) {
    va_list ap;
    va_start(ap, count);
    uint64_t total = 0;
    for (int i = 0; i < count; i++) {
        total += va_arg(ap, uint64_t);
    }
    va_end(ap);
    return total;
}

static void test_variadic(void) {
    memset(abi_probe_out, 0xEE, sizeof abi_probe_out);
    abi_probe_var(3, (uint64_t)10, (uint64_t)20, (uint64_t)30);
    CHECK(abi_probe_out[0] == 3 && abi_probe_out[1] == 10 && abi_probe_out[2] == 20 &&
          abi_probe_out[3] == 30,
          "variadic arguments are not on the stack at [sp]");
    CHECK(sum_variadic(4, (uint64_t)10, (uint64_t)20, (uint64_t)30, (uint64_t)40) == 100,
          "va_list traversal returned the wrong sum");
    printf("[PASS] Variadic argument handling: the 3 variadic arguments sit at [sp], [sp+8], [sp+16] and va_list is a char *\n");
}

int main(void) {
    printf("=== Native Calling Convention Verification (Host: Apple arm64) ===\n");
    test_int_args();
    test_fp_args();
    test_hfa();
    test_returns();
    test_stack_alignment();
    test_variadic();
    return 0;
}
