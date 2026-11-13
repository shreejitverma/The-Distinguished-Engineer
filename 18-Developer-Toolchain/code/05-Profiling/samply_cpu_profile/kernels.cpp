// Workload for the samply_cpu_profile lab.
//
// Four phases with known structure, so a profile of this program can be checked:
//   serial    one floating-point accumulator: a loop-carried dependency chain
//   unrolled  four accumulators: four independent chains over the same data
//   chase     one pointer-chasing loop run twice: over an in-order cycle (cache hits)
//             and over a scattered cycle through a 64 MiB table (cache misses)
//   sleep     no CPU work at all
//
// Usage: kernels <timings-file> [phase...]
// With no phase arguments every phase runs. Each phase appends "name wall_ns cpu_ns" to
// <timings-file>: wall time from steady_clock and this thread's CPU time from
// CLOCK_THREAD_CPUTIME_ID. stdout carries only values that do not vary between runs.

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <thread>
#include <vector>

#define NOINLINE __attribute__((noinline))
#define ALWAYS_INLINE inline __attribute__((always_inline))

namespace {

constexpr std::size_t kSumElems = 4096;      // 32 KiB of doubles: stays in L1D
constexpr long kSumReps = 80000;             // 327.68 million additions per sum phase
constexpr std::size_t kChaseElems = 1u << 24; // 16.7 million 4-byte links: 64 MiB
constexpr long kInOrderHops = 100000000;     // cheap hops, so many of them
constexpr long kScatteredHops = 1500000;     // expensive hops, so few of them
constexpr int kSleepMs = 200;

using Clock = std::chrono::steady_clock;

long long thread_cpu_ns() {
    timespec ts{};
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
    return static_cast<long long>(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}

// Measures one phase: wall time and the CPU time this thread spent in it.
class PhaseTimer {
public:
    PhaseTimer(std::FILE* out, const char* name)
        : out_(out), name_(name), wall_(Clock::now()), cpu_(thread_cpu_ns()) {}
    PhaseTimer(const PhaseTimer&) = delete;
    PhaseTimer& operator=(const PhaseTimer&) = delete;
    ~PhaseTimer() {
        const long long cpu = thread_cpu_ns() - cpu_;
        const long long wall =
            std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - wall_).count();
        std::fprintf(out_, "%s %lld %lld\n", name_, wall, cpu);
    }

private:
    std::FILE* out_;
    const char* name_;
    Clock::time_point wall_;
    long long cpu_;
};

}  // namespace

// One accumulator: every addition waits for the previous one.
NOINLINE double sum_serial(const double* p, std::size_t n) {
    double s = 0.0;
    for (std::size_t i = 0; i < n; ++i) s += p[i];
    return s;
}

// Four accumulators: four additions per iteration that do not depend on each other.
NOINLINE double sum_unrolled4(const double* p, std::size_t n) {
    double s0 = 0.0, s1 = 0.0, s2 = 0.0, s3 = 0.0;
    std::size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        s0 += p[i];
        s1 += p[i + 1];
        s2 += p[i + 2];
        s3 += p[i + 3];
    }
    for (; i < n; ++i) s0 += p[i];
    return (s0 + s1) + (s2 + s3);
}

// Always inlined: with debug info a profiler can still name it; without, it is gone.
static ALWAYS_INLINE std::uint32_t hop(const std::uint32_t* next, std::uint32_t at) {
    return next[at];
}

// The address of each load is the result of the previous load.
NOINLINE std::uint32_t chase(const std::uint32_t* next, std::uint32_t start, long hops) {
    std::uint32_t at = start;
    for (long i = 0; i < hops; ++i) at = hop(next, at);
    return at;
}

NOINLINE double drive_serial(std::vector<double>& v) {
    double total = 0.0;
    for (long r = 0; r < kSumReps; ++r) {
        v[static_cast<std::size_t>(r) & (kSumElems - 1)] += 1.0;  // defeat hoisting of the call
        total += sum_serial(v.data(), v.size());
    }
    return total;
}

NOINLINE double drive_unrolled(std::vector<double>& v) {
    double total = 0.0;
    for (long r = 0; r < kSumReps; ++r) {
        v[static_cast<std::size_t>(r) & (kSumElems - 1)] += 1.0;
        total += sum_unrolled4(v.data(), v.size());
    }
    return total;
}

// next[i] = i + 1: one cycle through the table in address order.
NOINLINE void build_in_order(std::vector<std::uint32_t>& next) {
    const std::uint32_t n = static_cast<std::uint32_t>(next.size());
    for (std::uint32_t i = 0; i < n; ++i) next[i] = (i + 1 == n) ? 0 : i + 1;
}

// next[i] = (a * i + c) mod 2^24 with a = 1 (mod 4) and c odd: a full-period linear
// congruential step (Hull and Dobell, 1962), so this is also one cycle through the whole
// table, but consecutive hops land megabytes apart.
NOINLINE void build_scattered(std::vector<std::uint32_t>& next) {
    const std::uint32_t n = static_cast<std::uint32_t>(next.size());
    for (std::uint32_t i = 0; i < n; ++i) next[i] = (i * 2654435761u + 1013904223u) & (n - 1);
}

NOINLINE std::uint32_t drive_chase_in_order(const std::vector<std::uint32_t>& next) {
    return chase(next.data(), 0, kInOrderHops);
}

NOINLINE std::uint32_t drive_chase_scattered(const std::vector<std::uint32_t>& next) {
    return chase(next.data(), 0, kScatteredHops);
}

NOINLINE void sleep_phase() {
    std::this_thread::sleep_for(std::chrono::milliseconds(kSleepMs));
}

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s <timings-file> [serial|unrolled|chase|sleep]...\n", argv[0]);
        return 2;
    }
    auto wanted = [&](const char* name) {
        if (argc == 2) return true;
        for (int i = 2; i < argc; ++i)
            if (std::strcmp(argv[i], name) == 0) return true;
        return false;
    };
    std::FILE* timings = std::fopen(argv[1], "w");
    if (timings == nullptr) {
        std::perror(argv[1]);
        return 1;
    }

    if (wanted("serial")) {
        std::vector<double> v(kSumElems, 1.0);
        double total = 0.0;
        {
            PhaseTimer timer(timings, "serial");
            total = drive_serial(v);
        }
        std::printf("serial   checksum %.1f\n", total);
    }
    if (wanted("unrolled")) {
        std::vector<double> v(kSumElems, 1.0);
        double total = 0.0;
        {
            PhaseTimer timer(timings, "unrolled");
            total = drive_unrolled(v);
        }
        std::printf("unrolled checksum %.1f\n", total);
    }
    if (wanted("chase")) {
        std::vector<std::uint32_t> next(kChaseElems);
        std::uint32_t in_order_end = 0, scattered_end = 0;
        build_in_order(next);
        {
            PhaseTimer timer(timings, "chase_in_order");
            in_order_end = drive_chase_in_order(next);
        }
        build_scattered(next);
        {
            PhaseTimer timer(timings, "chase_scattered");
            scattered_end = drive_chase_scattered(next);
        }
        std::printf("chase    in-order end %u, scattered end %u\n", in_order_end, scattered_end);
    }
    if (wanted("sleep")) {
        {
            PhaseTimer timer(timings, "sleep");
            sleep_phase();
        }
        std::printf("sleep    done\n");
    }
    std::fclose(timings);
    return 0;
}
