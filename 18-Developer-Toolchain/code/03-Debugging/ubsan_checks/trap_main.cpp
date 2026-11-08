// Overflows once in trap mode; the process must die with a signal.
#include <climits>
#include <cstdio>
extern "C" int add(int a, int b);
int main() {
    std::printf("before the overflow\n");
    std::fflush(stdout);
    volatile int big = INT_MAX;
    int r = add(big, 1);
    std::printf("after the overflow: %d\n", r);
    return 0;
}
