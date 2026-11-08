/* target.c - the program that minidbg launches, breaks, unwinds, and attaches to.
 *
 * Call chain: main -> outer_layer -> middle_layer -> leaf_compute, twice.
 * With seed = 2 the two leaf calls are leaf_compute(6, 4) and leaf_compute(9, 5),
 * so outer_layer returns (6*4 + 1 + 1) + (9*5 + 1 + 1) = 73 and the exit status is 73.
 *
 * The inputs come from volatile globals so the compiler cannot fold the calls away,
 * and NOIPA stops GCC from inlining, cloning (leaf_compute.constprop.0), or
 * changing the signature of a function, so every symbol the debugger looks up exists.
 */
#include <stdio.h>
#include <string.h>

#if defined(__clang__)
#define NOIPA __attribute__((noinline))
#else
#define NOIPA __attribute__((noipa))
#endif

volatile int seed = 2;
volatile int bias = 1;
volatile unsigned long spin_counter = 0;

NOIPA int leaf_compute(int a, int b) { return a * b + bias; }

/* The "+ 1" after the call keeps it a real call, not a tail call. */
NOIPA int middle_layer(int n) { return leaf_compute(n * 3, n + 2) + 1; }

NOIPA int outer_layer(int n) { return middle_layer(n) + middle_layer(n + 1); }

/* Attach mode: run forever so the debugger can PTRACE_SEIZE a process it did not start. */
NOIPA void spin_forever(void) {
    for (;;) {
        spin_counter = spin_counter + 1;
    }
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--spin") == 0) {
        spin_forever();
    }
    int r = outer_layer(seed);
    printf("target: outer_layer(%d) = %d\n", seed, r);
    return r;
}
