#define _GNU_SOURCE
#include <stdio.h>
#include <dlfcn.h>

typedef int (*compute_fn)(int, int);

int compute(int a, int b) {
    compute_fn real_compute = (compute_fn)dlsym(RTLD_NEXT, "compute");
    if (!real_compute) {
        fprintf(stderr, "interposer: failed to find real compute\n");
        return -1;
    }
    int orig = real_compute(a, b);
    printf("[interposer] intercepted compute(%d, %d) -> real result %d, returning %d\n", a, b, orig, orig * 2);
    return orig * 2;
}
