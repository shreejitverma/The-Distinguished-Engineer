#pragma once
#include <cstdint>

namespace workload {

// Prevents inlining and cloning across all GCC/Clang compilers
#if defined(__GNUC__) || defined(__clang__)
#define NOINLINE __attribute__((noinline, noclone))
#else
#define NOINLINE
#endif

extern volatile uint64_t g_sink;

NOINLINE void leaf_worker(uint64_t loop_count);
NOINLINE uint64_t task_alpha(uint64_t scale);
NOINLINE uint64_t task_beta(uint64_t scale);
NOINLINE uint64_t task_gamma(uint64_t scale);
NOINLINE void run_workload_round(uint64_t scale);

} // namespace workload
