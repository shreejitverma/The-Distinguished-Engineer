/* Reads the return address that a function saved in its own AArch64 frame record, and compares
 * it with the unsigned return address before and after stripping it with XPACLRI, the same
 * instruction the mini profiler's frame-pointer walker uses (sampler.cpp, strip_pac).
 * Prints one machine-readable line; run.sh turns it into derived facts. */
#include <stdint.h>
#include <stdio.h>

static uintptr_t strip_pac(uintptr_t addr) {
    register uintptr_t x30 __asm__("x30") = addr;
    __asm__ volatile("xpaclri" : "+r"(x30));
    return x30;
}

static volatile uintptr_t g_saved, g_unsigned;

__attribute__((noinline)) static void probe(void) {
    /* The frame record is two words at the frame pointer: caller's frame pointer, then the
     * link register value saved on entry, signed first when return-address signing is on. */
    const uintptr_t* record = (const uintptr_t*)__builtin_frame_address(0);
    g_saved = record[1];
    /* The compiler strips the code from __builtin_return_address on arm64e. */
    g_unsigned = (uintptr_t)__builtin_return_address(0);
}

__attribute__((noinline)) static int caller(void) {
    probe();
    return g_saved != 0; /* uses the result, so the call to probe is not a tail call */
}

int main(void) {
    if (!caller()) {
        return 1;
    }
    const uintptr_t stripped = strip_pac(g_saved);
    printf("signed=%s stripped_matches=%s\n", g_saved != g_unsigned ? "yes" : "no",
           stripped == g_unsigned ? "yes" : "no");
    return 0;
}
