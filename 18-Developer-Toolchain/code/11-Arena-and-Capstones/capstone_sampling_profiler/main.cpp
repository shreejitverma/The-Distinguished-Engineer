#include <iostream>
#include <map>
#include <string>
#include <vector>
#include "sampler.hpp"
#include "elf_symbolizer.hpp"
#include "workload.hpp"

int main(int argc, char* argv[]) {
    uint64_t interval_us = 1000; // 1 ms sampling period (1000 Hz)
    uint64_t rounds = 40;

    if (argc > 1) {
        rounds = std::stoull(argv[1]);
    }

    profiler::ElfSymbolizer symbolizer;
    if (!symbolizer.init()) {
        std::cerr << "Failed to initialize ELF symbolizer" << std::endl;
        return 1;
    }

    if (!profiler::Sampler::init(interval_us)) {
        std::cerr << "Failed to initialize sampler" << std::endl;
        return 1;
    }

    if (!profiler::Sampler::start()) {
        std::cerr << "Failed to start sampling timer" << std::endl;
        return 1;
    }

    // Execute known synthetic workload
    for (uint64_t r = 0; r < rounds; ++r) {
        workload::run_workload_round(1);
    }

    profiler::Sampler::stop();

    auto raw_samples = profiler::Sampler::get_samples();
    std::map<std::string, uint64_t> folded_counts;

    for (const auto& sample : raw_samples) {
        if (sample.depth == 0) continue;

        // Resolve addresses to symbol names
        std::vector<std::string> frames;
        frames.reserve(sample.depth);
        for (size_t i = 0; i < sample.depth; ++i) {
            // ips[0] is the interrupted PC; every later entry is a return address, which points
            // at the instruction after the call, so look up ip - 1 to stay inside the caller.
            const uintptr_t ip = (i == 0) ? sample.ips[i] : sample.ips[i] - 1;
            std::string name = symbolizer.resolve(ip);
            frames.push_back(name);
        }

        // Standard folded stack format is root-to-leaf:
        // sample.ips[0] was the interrupted leaf PC, sample.ips[depth-1] is the root caller.
        // Therefore, we traverse frames backwards from depth-1 down to 0.
        std::string folded_stack;
        for (size_t i = frames.size(); i > 0; --i) {
            if (!folded_stack.empty()) {
                folded_stack += ";";
            }
            folded_stack += frames[i - 1];
        }

        folded_counts[folded_stack]++;
    }

    // Emit standard folded stack format
    for (const auto& kv : folded_counts) {
        std::cout << kv.first << " " << kv.second << "\n";
    }

    return 0;
}
