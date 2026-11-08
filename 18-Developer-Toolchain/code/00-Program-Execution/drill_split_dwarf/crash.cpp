#include <cstdio>
#include <cstdlib>

// Leaf inlined function that performs the faulting memory store
static __attribute__((always_inline)) inline void level3_leaf(volatile int *ptr) {
    *ptr = 1337;
}

// Intermediate inlined risk processing layer
static __attribute__((always_inline)) inline void level2_process(volatile int *ptr) {
    level3_leaf(ptr);
}

// Top inlined dispatch layer
static __attribute__((always_inline)) inline void level1_dispatch(volatile int *ptr) {
    level2_process(ptr);
}

// Out-of-line physical worker frame
__attribute__((noinline)) void physical_worker(volatile int *ptr) {
    level1_dispatch(ptr);
}

int main(int argc, char **argv) {
    volatile int *bad_ptr = nullptr;
    physical_worker(bad_ptr);
    return 0;
}
