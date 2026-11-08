// The optimizer may assume signed overflow never happens, so x + 1 > x
// can be folded to true; -fwrapv defines wrapping and forbids the fold.
#include <climits>
#include <cstdio>
#include <cstdlib>

__attribute__((noinline)) bool plus_one_is_greater(int x) {
    return x + 1 > x;
}

int main(int argc, char** argv) {
    int x = argc > 1 ? std::atoi(argv[1]) : INT_MAX;
    std::printf("plus_one_is_greater(%d) = %d\n", x, plus_one_is_greater(x));
    return 0;
}
