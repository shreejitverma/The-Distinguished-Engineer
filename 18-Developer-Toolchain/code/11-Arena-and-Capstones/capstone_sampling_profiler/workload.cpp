#include "workload.hpp"

namespace workload {

volatile uint64_t g_sink = 0;

NOINLINE void leaf_worker(uint64_t loop_count) {
    uint64_t val = 0x9e3779b97f4a7c15ULL;
    for (uint64_t i = 0; i < loop_count; ++i) {
        val ^= (val << 13);
        val ^= (val >> 7);
        val ^= (val << 17);
        val += i;
    }
    g_sink += val;
}

// task_alpha executes 5 units of work (50% target)
NOINLINE uint64_t task_alpha(uint64_t scale) {
    leaf_worker(scale * 500000ULL);
    return scale * 5;
}

// task_beta executes 3 units of work (30% target)
NOINLINE uint64_t task_beta(uint64_t scale) {
    leaf_worker(scale * 300000ULL);
    return scale * 3;
}

// task_gamma executes 2 units of work (20% target)
NOINLINE uint64_t task_gamma(uint64_t scale) {
    leaf_worker(scale * 200000ULL);
    return scale * 2;
}

NOINLINE void run_workload_round(uint64_t scale) {
    task_alpha(scale);
    task_beta(scale);
    task_gamma(scale);
}

} // namespace workload
