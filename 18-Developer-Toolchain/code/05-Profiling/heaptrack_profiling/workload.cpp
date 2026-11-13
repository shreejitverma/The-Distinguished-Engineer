// One workload, many heap behaviors: each mode isolates one thing a heap
// profiler or an allocator's own telemetry is supposed to reveal.
//
//   none                 do nothing (baseline for start-up allocations)
//   churn N              one short-lived std::vector per iteration
//   reuse N              the same loop with one vector hoisted out of it
//   pmr N                the same loop on a stack buffer through std::pmr
//   peakleak             a 64 MiB transient peak, then 3 x 4000 bytes leaked
//   peak                 a 64 MiB transient peak, everything freed
//   small N              N live malloc(1) blocks, then freed
//   dhat N               churn N plus 100 write-only 4096-byte buffers
//   telemetry            allocator statistics and RSS (glibc or jemalloc)
//
// Built with -O0 so that no allocation is elided and every frame is visible.
// The profiled modes print nothing: stdout's buffer would be one more
// allocation in the profile.

#include <dlfcn.h>
#include <malloc.h>
#include <unistd.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory_resource>
#include <string>
#include <vector>

namespace {

volatile double g_sink = 0.0;  // keeps every loop's result observable

// The hot-path pattern heaptrack calls "temporary": allocate, use, free,
// with no other allocation in between.
double parse_record(int i) {
    std::vector<double> fields(8);
    for (int k = 0; k < 8; ++k) fields[k] = static_cast<double>(i + k);
    double sum = 0.0;
    for (int k = 0; k < 8; ++k) sum += fields[k];
    return sum;
}

double parse_record_reuse(int i, std::vector<double>& fields) {
    for (int k = 0; k < 8; ++k) fields[k] = static_cast<double>(i + k);
    double sum = 0.0;
    for (int k = 0; k < 8; ++k) sum += fields[k];
    return sum;
}

void run_churn(int n) {
    for (int i = 0; i < n; ++i) g_sink = g_sink + parse_record(i);
}

void run_reuse(int n) {
    std::vector<double> fields(8);  // one allocation for the whole loop
    for (int i = 0; i < n; ++i) g_sink = g_sink + parse_record_reuse(i, fields);
}

void run_pmr(int n) {
    alignas(std::max_align_t) unsigned char buffer[256];
    // null_memory_resource as upstream: overflowing the stack buffer throws
    // std::bad_alloc instead of silently falling back to the heap.
    std::pmr::monotonic_buffer_resource arena(buffer, sizeof buffer,
                                              std::pmr::null_memory_resource());
    for (int i = 0; i < n; ++i) {
        {
            std::pmr::vector<double> fields(8, &arena);
            for (int k = 0; k < 8; ++k) fields[k] = static_cast<double>(i + k);
            double sum = 0.0;
            for (int k = 0; k < 8; ++k) sum += fields[k];
            g_sink = g_sink + sum;
        }
        arena.release();  // rewind the bump pointer; no free() is ever called
    }
}

void transient_peak() {
    std::vector<char> big(64u << 20);  // 64 MiB, value-initialized (touched)
    g_sink = g_sink + big[12345];
}

char* g_leaks[3];

void leak_on_purpose() {
    for (char*& p : g_leaks) {
        p = new char[4000];
        std::memset(p, 1, 4000);
    }
    // Overwrite the only copies of the pointers: the blocks are now unreachable.
    for (char*& p : g_leaks) p = nullptr;
}

void* g_small[100000];

void hold_small_blocks(int n) {
    for (int i = 0; i < n; ++i) g_small[i] = std::malloc(1);
    for (int i = 0; i < n; ++i) std::free(g_small[i]);
}

// Written in full and never read: DHAT's "zero reads" class.
void write_only_buffer() {
    void* p = std::malloc(4096);
    std::memset(p, 0x5a, 4096);
    std::free(p);
}

// ---------------------------------------------------------------- telemetry

using mallctl_fn = int (*)(const char*, void*, size_t*, void*, size_t);

constexpr int kBlocks = 300000;
void* g_blocks[kBlocks];
constexpr double kMiB = 1024.0 * 1024.0;

long rss_bytes() {
    FILE* f = std::fopen("/proc/self/statm", "r");
    if (f == nullptr) return -1;
    long size = 0, resident = 0;
    int got = std::fscanf(f, "%ld %ld", &size, &resident);
    std::fclose(f);
    if (got != 2) return -1;
    return resident * sysconf(_SC_PAGESIZE);
}

const char* yes_no(bool b) { return b ? "yes" : "no"; }

size_t je_stat(mallctl_fn mallctl, const char* name) {
    uint64_t epoch = 1;
    size_t elen = sizeof epoch;
    // Statistics are cached; writing "epoch" refreshes them.
    if (mallctl("epoch", &epoch, &elen, &epoch, elen) != 0) std::abort();
    size_t value = 0;
    size_t len = sizeof value;
    if (mallctl(name, &value, &len, nullptr, 0) != 0) {
        std::fprintf(stderr, "mallctl(%s) failed\n", name);
        std::abort();
    }
    return value;
}

// In-use bytes as the allocator itself counts them. Signed, because a delta
// against an earlier baseline can go negative when caches are flushed.
long long in_use(mallctl_fn je) {
    if (je != nullptr) return static_cast<long long>(je_stat(je, "stats.allocated"));
    return static_cast<long long>(mallinfo2().uordblks);
}

void run_telemetry() {
    auto je = reinterpret_cast<mallctl_fn>(dlsym(RTLD_DEFAULT, "mallctl"));
    std::printf("allocator: %s\n", je != nullptr ? "jemalloc" : "glibc");
    std::fflush(stdout);  // stdout's buffer is allocated before any baseline
    std::memset(g_blocks, 0, sizeof g_blocks);  // fault the pointer array in now

    // 1. Fragmentation: 300000 x 64 bytes, free every other block, then the rest.
    // This runs first so that no earlier free chunks or empty slabs exist for
    // the trim and purge steps to find.
    long long base = in_use(je);
    long rss0 = rss_bytes();
    for (int i = 0; i < kBlocks; ++i) {
        g_blocks[i] = std::malloc(64);
        std::memset(g_blocks[i], 7, 64);
    }
    long long full = in_use(je) - base;
    long rss1 = rss_bytes();
    std::printf("after 300000 x malloc(64): in use %.1f MiB (%.0f bytes per block), RSS grew more than 10 MiB: %s\n",
                full / kMiB, static_cast<double>(full) / kBlocks, yes_no(rss1 - rss0 > (10L << 20)));

    for (int i = 1; i < kBlocks; i += 2) std::free(g_blocks[i]);
    long long half = in_use(je) - base;
    long rss2 = rss_bytes();
    std::printf("after freeing every other block: in use %.1f MiB, RSS dropped more than 1 MiB: %s\n",
                half / kMiB, yes_no(rss1 - rss2 > (1L << 20)));
    if (je != nullptr) {
        double ratio = static_cast<double>(je_stat(je, "stats.active")) /
                       static_cast<double>(je_stat(je, "stats.allocated"));
        std::printf("  jemalloc stats.active / stats.allocated: %.1f\n", ratio);
    } else {
        struct mallinfo2 mi = mallinfo2();
        std::printf("  glibc free chunks below the top chunk: %.1f MiB\n",
                    (mi.fordblks - mi.keepcost) / kMiB);
    }

    // Ask the allocator to give memory back while the heap is half full.
    if (je != nullptr) {
        if (je("arena.4096.purge", nullptr, nullptr, nullptr, 0) != 0) std::abort();
    } else {
        malloc_trim(0);
    }
    long rss3 = rss_bytes();
    std::printf("  explicit purge or trim with the heap half full: RSS dropped more than 1 MiB: %s\n",
                yes_no(rss2 - rss3 > (1L << 20)));

    for (int i = 0; i < kBlocks; i += 2) std::free(g_blocks[i]);
    long rss4 = rss_bytes();
    std::printf("after freeing the rest: in use %.1f MiB, RSS dropped more than 10 MiB: %s\n",
                (in_use(je) - base) / kMiB, yes_no(rss3 - rss4 > (10L << 20)));

    if (je != nullptr) {
        if (je("arena.4096.purge", nullptr, nullptr, nullptr, 0) != 0) std::abort();
    } else {
        malloc_trim(0);
    }
    long rss5 = rss_bytes();
    std::printf("  explicit purge or trim with the heap empty: RSS dropped more than 10 MiB: %s\n",
                yes_no(rss4 - rss5 > (10L << 20)));

    // 2. Per-block cost of malloc(1), as the allocator itself counts it.
    long long before = in_use(je);
    for (int i = 0; i < 100000; ++i) g_small[i] = std::malloc(1);
    long long after = in_use(je);
    std::printf("malloc(1): usable size %zu, in-use bytes per block %.0f\n",
                malloc_usable_size(g_small[0]),
                static_cast<double>(after - before) / 100000.0);
    for (int i = 0; i < 100000; ++i) std::free(g_small[i]);
}

int arg_int(int argc, char** argv, int fallback) {
    return argc > 2 ? std::atoi(argv[2]) : fallback;
}

}  // namespace

int main(int argc, char** argv) {
    const std::string mode = argc > 1 ? argv[1] : "none";
    const int n = arg_int(argc, argv, 100000);
    if (mode == "none") {
    } else if (mode == "churn") {
        run_churn(n);
    } else if (mode == "reuse") {
        run_reuse(n);
    } else if (mode == "pmr") {
        run_pmr(n);
    } else if (mode == "peakleak") {
        transient_peak();
        leak_on_purpose();
    } else if (mode == "peak") {
        transient_peak();
    } else if (mode == "small") {
        hold_small_blocks(n > 100000 ? 100000 : n);
    } else if (mode == "dhat") {
        run_churn(n);
        for (int i = 0; i < 100; ++i) write_only_buffer();
    } else if (mode == "telemetry") {
        run_telemetry();
    } else {
        std::fprintf(stderr, "unknown mode: %s\n", mode.c_str());
        return 2;
    }
    // Exit sentinel, identical in every mode. At exit heaptrack calls
    // __gnu_cxx::__freeres(), which frees libstdc++'s start-up emergency pool.
    // heaptrack counts a free as temporary when no event separates it from the
    // matching allocation, so in an otherwise empty run that one free would be
    // "temporary" and skew every delta against the baseline by one. A last
    // allocate-and-free pair makes the pool's free non-temporary in all modes.
    std::free(std::malloc(1));
    return 0;
}
