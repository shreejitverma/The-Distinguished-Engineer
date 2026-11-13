// Profiling target with a known answer.
//
// Every stage spends its CPU time in the same leaf, burn(), so a flat profile says only
// "burn: 100%". The split between callers is fixed by the unit counts below, which is what
// lets the lab check a folded profile against ground truth instead of eyeballing it.
//
//   mode "pipeline":  decode 5 units, match 3 units, report 2 units (through 3 recursive frames)
//   mode "pitfalls":  via_tailcall 6 units (tail call), via_inline 4 units (inlined helper)
//
// usage: target MODE RAW_OUT TARGET_SAMPLES [INTERVAL_US]   (profile itself with the SIGPROF sampler)
//        target MODE - ROUNDS                               (run ROUNDS rounds unprofiled, for perf_ring)
#include "sampler.hpp"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#if defined(__GNUC__) && !defined(__clang__)
#define MINIPROF_NOINLINE __attribute__((noinline, noclone))
#else
#define MINIPROF_NOINLINE __attribute__((noinline))
#endif

namespace {

volatile std::uint64_t g_sink = 0;

// The only place CPU time is spent. A leaf: it calls nothing, so whether it gets a frame
// record is decided by the compiler's leaf-frame-pointer setting, not by the source.
MINIPROF_NOINLINE std::uint64_t burn(unsigned units) {
    std::uint64_t x = 88172645463325252ULL;
#if defined(MINIPROF_OTHER_BUILD)
    // run.sh builds this variant to stand for "a later build of the same source".
    const std::uint64_t iterations = static_cast<std::uint64_t>(units) * 200001ULL;
#else
    const std::uint64_t iterations = static_cast<std::uint64_t>(units) * 200000ULL;
#endif
    for (std::uint64_t i = 0; i < iterations; ++i) {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
    }
    return x;
}

} // namespace

namespace pipeline {

// Each stage uses the result after the call, so the call cannot become a tail call.
MINIPROF_NOINLINE std::uint64_t decode(unsigned units) {
    const std::uint64_t r = burn(units);
    g_sink = g_sink + r;
    return r + 1;
}

MINIPROF_NOINLINE std::uint64_t match(unsigned units) {
    const std::uint64_t r = burn(units);
    g_sink = g_sink + r;
    return r + 2;
}

MINIPROF_NOINLINE std::uint64_t report(unsigned depth, unsigned units) {
    const std::uint64_t r = depth > 1 ? report(depth - 1, units) : burn(units);
    g_sink = g_sink + r;
    return r + 3;
}

// "return burn(units);" in tail position: at -O2 the call becomes a jump and this frame is gone.
MINIPROF_NOINLINE std::uint64_t via_tailcall(unsigned units) { return burn(units); }

// Inlined into via_inline: there is no call, so no frame record and no symbol-table entry.
__attribute__((always_inline)) inline std::uint64_t inlined_helper(unsigned units) {
    const std::uint64_t r = burn(units);
    g_sink = g_sink + r;
    return r + 4;
}

MINIPROF_NOINLINE std::uint64_t via_inline(unsigned units) {
    const std::uint64_t r = inlined_helper(units);
    g_sink = g_sink + r;
    return r + 5;
}

} // namespace pipeline

// One round. The scale cycles through 1, 2, 3: it changes the length of a round without
// changing the ratio between its stages, so no fixed sampling period stays in step with the rounds.
MINIPROF_NOINLINE static std::uint64_t run_round(bool pitfalls) {
    static unsigned round = 0;
    const unsigned scale = 1 + round++ % 3;
    std::uint64_t acc = 0;
    if (pitfalls) {
        acc += pipeline::via_tailcall(6 * scale);
        acc += pipeline::via_inline(4 * scale);
    } else {
        acc += pipeline::decode(5 * scale);
        acc += pipeline::match(3 * scale);
        acc += pipeline::report(3, 2 * scale);
    }
    g_sink = g_sink + acc;
    return acc;
}

// Parses a positive decimal count; returns 0 for anything else.
static unsigned parse_count(const char* text) {
    char* end = nullptr;
    const unsigned long value = std::strtoul(text, &end, 10);
    return (end == text || *end != '\0' || value > 100000000UL) ? 0 : static_cast<unsigned>(value);
}

int main(int argc, char** argv) {
    if (argc != 4 && argc != 5) {
        std::fprintf(stderr, "usage: %s pipeline|pitfalls RAW_OUT|- COUNT [INTERVAL_US]\n", argv[0]);
        return 2;
    }
    const bool pitfalls = std::strcmp(argv[1], "pitfalls") == 0;
    const bool profiled = std::strcmp(argv[2], "-") != 0;
    const unsigned count = parse_count(argv[3]);
    if (count == 0) {
        std::fprintf(stderr, "COUNT must be a positive integer\n");
        return 2;
    }
    constexpr unsigned kMaxRounds = 20000; // a hard stop if the timer never fires

    if (!profiled) {
        for (unsigned round = 0; round < count; ++round) {
            run_round(pitfalls);
        }
        return 0;
    }

    miniprof::Options opts;
    if (argc == 5) {
        opts.interval_us = parse_count(argv[4]);
        if (opts.interval_us == 0) {
            std::fprintf(stderr, "INTERVAL_US must be a positive integer\n");
            return 2;
        }
    }
    if (count >= opts.capacity) {
        std::fprintf(stderr, "target sample count %u does not fit the sample buffer\n", count);
        return 2;
    }
    if (!miniprof::start(opts)) {
        std::perror("miniprof::start");
        return 1;
    }
    // Whole rounds only, so the unit ratio between stages is exact whatever the timer rate is.
    unsigned rounds = 0;
    while (miniprof::samples_taken() < count && rounds < kMaxRounds) {
        run_round(pitfalls);
        ++rounds;
    }
    if (!miniprof::stop_and_write(argv[2])) {
        std::perror("miniprof::stop_and_write");
        return 1;
    }
    return 0;
}
