// guard_probe.cpp - shows that the trace-pc-guard callback is called unconditionally.
// The callback zeroes each guard on its first hit, as the sample in the SanitizerCoverage
// documentation does, and counts the calls that still arrive with a zeroed guard.
// A compiler that wrapped the call in `if (*guard)` would leave that second count at 0.
// Only record.cpp is instrumented; this file is compiled without -fsanitize-coverage.
#include <cstdint>
#include <cstdio>

#include "record.h"

namespace {
// Constant-initialized on purpose: the init callback runs before dynamic initializers.
uint32_t g_guards = 0;
unsigned long g_armed_calls = 0;   // calls that arrived with a nonzero guard
unsigned long g_zeroed_calls = 0;  // calls that arrived after the guard was set to 0
}  // namespace

extern "C" void __sanitizer_cov_trace_pc_guard_init(uint32_t *start, uint32_t *stop) {
    if (start == stop || *start) return;  // already initialized
    for (uint32_t *g = start; g < stop; ++g) *g = ++g_guards;
}

extern "C" void __sanitizer_cov_trace_pc_guard(uint32_t *guard) {
    if (*guard == 0) {
        ++g_zeroed_calls;
        return;
    }
    ++g_armed_calls;
    *guard = 0;
}

int main() {
    const uint8_t record[] = {'F', 'U', 'Z', 'Z', 2, 'h', 'i'};
    uint8_t out[16];
    std::printf("guard probe: %u guards registered for record.cpp\n", g_guards);
    for (int run = 1; run <= 3; ++run) {
        parse_record(record, sizeof(record), out, sizeof(out));
        std::printf("guard probe: after run %d of one valid record: %lu call(s) with an armed guard, %lu with a zeroed guard\n",
                    run, g_armed_calls, g_zeroed_calls);
    }
    return 0;
}
