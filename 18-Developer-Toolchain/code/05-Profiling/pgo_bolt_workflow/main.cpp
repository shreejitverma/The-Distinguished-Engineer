// main.cpp - driver for workload.cpp.
//
// Usage: workload <train|eval> <iterations>
// The train and eval inputs use different seeds with the same message mix, so a
// profile collected on "train" is applied to a binary that is measured on "eval".

#include <cstdio>
#include <cstdlib>
#include <cstring>

extern "C" unsigned long long run_batch(unsigned long long seed, unsigned long long n);
extern "C" unsigned long long dump_state(unsigned long long x);
extern "C" unsigned long long g_bad;

int main(int argc, char** argv) {
    if (argc != 3 || (std::strcmp(argv[1], "train") != 0 && std::strcmp(argv[1], "eval") != 0)) {
        std::fprintf(stderr, "usage: %s <train|eval> <iterations>\n", argv[0]);
        return 2;
    }
    const bool train = std::strcmp(argv[1], "train") == 0;
    char* end = nullptr;
    const unsigned long long n = std::strtoull(argv[2], &end, 10);
    if (end == argv[2] || *end != '\0' || n == 0) {
        std::fprintf(stderr, "iterations must be a positive integer\n");
        return 2;
    }
    const unsigned long long seed = train ? 0x1234567ull : 0x89abcdefull;
    unsigned long long acc = run_batch(seed, n);
    if (std::getenv("WORKLOAD_DUMP") != nullptr) {
        acc ^= dump_state(acc);
    }
    std::printf("checksum=%016llx bad=%llu\n", acc, g_bad);
    return 0;
}
