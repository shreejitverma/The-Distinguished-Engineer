#pragma once
#include <cstdint>
#include <cstddef>
#include <vector>

namespace profiler {

constexpr size_t MAX_STACK_DEPTH = 64;
constexpr size_t MAX_SAMPLES = 20000;

struct RawSample {
    size_t depth;
    uintptr_t ips[MAX_STACK_DEPTH];
};

class Sampler {
public:
    static bool init(uint64_t interval_us);
    static bool start();
    static void stop();
    static std::vector<RawSample> get_samples();
    static size_t dropped_samples();
};

} // namespace profiler
