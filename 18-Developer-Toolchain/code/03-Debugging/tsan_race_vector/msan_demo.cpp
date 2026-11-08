// MemorySanitizer stage of the lab; built only when the compiler accepts -fsanitize=memory.
// Copying and arithmetic on uninitialized bytes are silent; the branch is what gets reported.
#include <cstdio>
#include <cstdlib>

__attribute__((noinline)) int pick(const int *p, int i) { return p[i]; }

int main(int argc, char **) {
    int *p = static_cast<int *>(std::malloc(4 * sizeof(int)));
    p[0] = argc;
    int copy = pick(p, argc);  // p[1] was never written: shadow is copied, no report yet
    int sum = copy + 1;        // shadow propagates through the add, still no report
    if (sum > 0) {             // a branch depends on poisoned bits: report here
        std::puts("positive");
    }
    std::free(p);
    return 0;
}
