#include <stddef.h>

/* Scenario 1: Independent memory buffers allow auto-vectorization via restrict. */
void vector_add(float * restrict a, const float * restrict b, const float * restrict c, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        a[i] = b[i] + c[i];
    }
}

/* Scenario 2: Backward loop-carried dependency prevents vectorization. */
void dependent_add(float *a, const float *b, size_t n) {
    for (size_t i = 1; i < n; ++i) {
        a[i] = a[i - 1] + b[i];
    }
}

/* Scenario 3: Small static helper with single callsite qualifies for inlining. */
static int helper_inline(int x) {
    return x * 7 + 3;
}

int call_inline(int val) {
    return helper_inline(val) * 2;
}

/* Scenario 4: Explicit noinline attribute inhibits inlining and forces call linkage. */
__attribute__((noinline)) int helper_noinline(int x) {
    return x * 7 + 3;
}

int call_noinline(int val) {
    return helper_noinline(val) * 2;
}
