// One always_inline helper overflows in three callers: how many reports?
#include <climits>
#include <cstdio>
static inline __attribute__((always_inline)) int bump(int k) { return k + 1; }
__attribute__((noinline)) int via_a(int k) { return bump(k); }
__attribute__((noinline)) int via_b(int k) { return bump(k); }
__attribute__((noinline)) int via_c(int k) { return bump(k); }
int main() {
    volatile int m = INT_MAX;
    std::printf("%d %d %d\n", via_a(m), via_b(m), via_c(m));
    return 0;
}
