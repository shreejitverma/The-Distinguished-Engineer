/*
 * Workload with a known answer for a profiler.
 *
 * middle() spends three units of work in hot_leaf() and one unit in warm_leaf(), and the two
 * loops have the same shape, so a correct sampling profiler must report about 75% and 25%.
 *
 * Two details are deliberate:
 * - Both leaves are small enough that the compiler gives them no frame record, so the only
 *   way a frame-pointer walker can see middle() above hot_leaf() is the saved link register.
 * - middle() ends with a call to warm_leaf(), which the optimizer turns into a tail call, so
 *   middle() is not on the stack while warm_leaf() runs.
 *
 * The whole computation sits inside one os_signpost interval in the Points of Interest
 * category, which the Time Profiler template records.
 */
#include <os/log.h>
#include <os/signpost.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define UNIT 2000000ULL

__attribute__((noinline)) static uint64_t hot_leaf(uint64_t x, uint64_t n) {
    for (uint64_t i = 0; i < n; i++) {
        x = x * 6364136223846793005ULL + 1442695040888963407ULL;
        x ^= x >> 29;
    }
    return x;
}

__attribute__((noinline)) static uint64_t warm_leaf(uint64_t x, uint64_t n) {
    for (uint64_t i = 0; i < n; i++) {
        x = x * 2862933555777941757ULL + 3037000493ULL;
        x ^= x >> 31;
    }
    return x;
}

__attribute__((noinline)) static uint64_t middle(uint64_t x) {
    x = hot_leaf(x, 3 * UNIT);
    return warm_leaf(x, UNIT);
}

int main(int argc, char **argv) {
    uint64_t rounds = argc > 1 ? strtoull(argv[1], NULL, 10) : 120;
    os_log_t log = os_log_create("lab.xctrace", OS_LOG_CATEGORY_POINTS_OF_INTEREST);
    os_signpost_id_t sid = os_signpost_id_generate(log);
    uint64_t x = 1;

    os_signpost_interval_begin(log, sid, "compute", "rounds=%llu", (unsigned long long)rounds);
    for (uint64_t r = 0; r < rounds; r++) {
        x = middle(x);
    }
    os_signpost_interval_end(log, sid, "compute");

    printf("checksum %llu\n", (unsigned long long)(x & 0xff));
    return 0;
}
