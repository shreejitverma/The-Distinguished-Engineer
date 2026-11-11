// toyfuzz.cpp - a small coverage-guided fuzzer built on SanitizerCoverage trace-pc-guard.
// It rebuilds the libFuzzer loop: pick a corpus input, mutate it, run it, keep it if it
// covered a new edge, and dump the input from the sanitizer death callback on a crash.
// Usage: toyfuzz guided|blind <max_execs> <crash_file>
// Only record.cpp is instrumented; this file is compiled without -fsanitize-coverage.
#include <sanitizer/common_interface_defs.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <vector>

#include "record.h"

namespace {
using Bytes = std::vector<uint8_t>;

// Plain pointers, not std::vector: the coverage module constructor calls
// __sanitizer_cov_trace_pc_guard_init before this file's dynamic initializers run,
// and a vector's constructor would then wipe what the callback had stored.
uint32_t g_num_guards = 0;
uint8_t *g_hit = nullptr;   // edges hit by the current execution, indexed by guard id
uint8_t *g_seen = nullptr;  // edges hit by any execution so far
const Bytes *g_current = nullptr;
unsigned long g_execs = 0;  // executions started, including the seed
const char *g_crash_path = nullptr;

uint64_t g_rng = 0x9E3779B97F4A7C15ULL;  // fixed seed: the run is reproducible
uint64_t next_rand() {                    // splitmix64
    uint64_t z = (g_rng += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}
size_t below(size_t n) { return static_cast<size_t>(next_rand() % n); }

void mutate(Bytes &b) {
    constexpr size_t kMaxLen = 64;
    const size_t ops = 1 + below(3);
    for (size_t k = 0; k < ops; ++k) {
        switch (below(4)) {
            case 0:  // overwrite one byte with a random value
                if (!b.empty()) b[below(b.size())] = static_cast<uint8_t>(next_rand());
                break;
            case 1:  // insert one random byte
                if (b.size() < kMaxLen) b.insert(b.begin() + below(b.size() + 1), static_cast<uint8_t>(next_rand()));
                break;
            case 2:  // erase one byte
                if (b.size() > 1) b.erase(b.begin() + below(b.size()));
                break;
            default:  // append a run of 1 to 32 repeated bytes
                b.insert(b.end(), 1 + below(32), static_cast<uint8_t>(next_rand()));
                if (b.size() > kMaxLen) b.resize(kMaxLen);
                break;
        }
    }
}

// How far into the format an input gets: magic bytes matched, plus 1 if the length check passes.
int depth(const Bytes &b) {
    static const char kMagic[] = "FUZZ";
    int d = 0;
    while (d < 4 && static_cast<size_t>(d) < b.size() && b[d] == static_cast<uint8_t>(kMagic[d])) ++d;
    if (d == 4 && b.size() >= 5 && b[4] <= b.size() - 5) ++d;
    return d;
}

void on_death() {
    if (g_current == nullptr || g_crash_path == nullptr) return;
    if (FILE *f = std::fopen(g_crash_path, "wb")) {
        std::fwrite(g_current->data(), 1, g_current->size(), f);
        std::fclose(f);
    }
    std::printf("toyfuzz: death callback saved the %zu-byte input of execution %lu\n", g_current->size(), g_execs);
    std::fflush(stdout);
}

// Runs one input and returns the number of edges it covered for the first time.
size_t execute(const Bytes &b) {
    std::memset(g_hit, 0, g_num_guards + 1);
    g_current = &b;
    ++g_execs;
    auto out = std::make_unique<uint8_t[]>(16);
    parse_record(b.data(), b.size(), out.get(), 16);
    size_t fresh = 0;
    for (uint32_t i = 1; i <= g_num_guards; ++i) {
        if (g_hit[i] && !g_seen[i]) {
            g_seen[i] = 1;
            ++fresh;
        }
    }
    return fresh;
}
}  // namespace

// SanitizerCoverage calls this once per instrumented module with that module's guard array.
extern "C" void __sanitizer_cov_trace_pc_guard_init(uint32_t *start, uint32_t *stop) {
    if (start == stop || *start) return;  // already initialized
    for (uint32_t *g = start; g < stop; ++g) *g = ++g_num_guards;
    g_hit = static_cast<uint8_t *>(std::calloc(g_num_guards + 1, 1));
    g_seen = static_cast<uint8_t *>(std::calloc(g_num_guards + 1, 1));
}

// SanitizerCoverage calls this on every instrumented edge; the guard holds that edge's id.
extern "C" void __sanitizer_cov_trace_pc_guard(uint32_t *guard) {
    if (*guard) g_hit[*guard] = 1;
}

int main(int argc, char **argv) {
    if (argc != 4) {
        std::fprintf(stderr, "usage: %s guided|blind <max_execs> <crash_file>\n", argv[0]);
        return 2;
    }
    const bool guided = std::strcmp(argv[1], "guided") == 0;
    const unsigned long max_execs = std::strtoul(argv[2], nullptr, 10);
    g_crash_path = argv[3];
    __sanitizer_set_death_callback(on_death);

    std::vector<Bytes> corpus{Bytes{'A', 'A', 'A', 'A', 'A'}};
    int deepest = depth(corpus[0]);
    execute(corpus[0]);
    for (unsigned long n = 1; n <= max_execs; ++n) {
        Bytes child = corpus[below(corpus.size())];
        mutate(child);
        if (depth(child) > deepest) deepest = depth(child);
        const size_t fresh = execute(child);
        if (guided && fresh > 0) {
            std::printf("%s: execution %lu added corpus entry %zu (%zu new edge(s), format depth %d)\n", argv[1], g_execs,
                        corpus.size(), fresh, depth(child));
            std::fflush(stdout);  // the next input may crash the process
            corpus.push_back(std::move(child));
        }
    }
    std::printf("%s: no crash in %lu executions; corpus size %zu; deepest format depth reached %d of 5\n", argv[1],
                max_execs, corpus.size(), deepest);
    return 0;
}
