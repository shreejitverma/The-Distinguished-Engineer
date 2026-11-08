#include <stdio.h>

/* Address taken below, so --icf=safe must keep these two apart; --icf=all folds them. */
__attribute__((noinline)) int algo_a(int x) {
    return x * 100 + 42;
}

__attribute__((noinline)) int algo_b(int x) {
    return x * 100 + 42;
}

/* Only ever called directly, so --icf=safe may fold them. */
__attribute__((noinline)) int twin_a(int x) {
    return x * 7 + 3;
}

__attribute__((noinline)) int twin_b(int x) {
    return x * 7 + 3;
}

int main(int argc, char **argv) {
    (void)argv;
    void * volatile pa = (void*)algo_a;
    void * volatile pb = (void*)algo_b;
    if (pa == pb) {
        printf("Pointers are identical (folded)\n");
    } else {
        printf("Pointers are distinct (safe)\n");
    }
    volatile int sink = twin_a(argc) + twin_b(argc);
    (void)sink;
    return 0;
}
