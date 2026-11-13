// Part 2 of the lab: the same experiments written against Google Benchmark.
// summarize_gb.py turns the JSON this binary writes into derived facts.
#include <benchmark/benchmark.h>

#include <algorithm>
#include <vector>

#include "workloads.hpp"

namespace {

void BM_SumDiscarded(benchmark::State& state) {
  const std::vector<int> v = lab::shuffled(4096);
  for (auto _ : state) {
    lab::sum_squares_discarded(v);
  }
}
BENCHMARK(BM_SumDiscarded);

void BM_SumDoNotOptimize(benchmark::State& state) {
  const std::vector<int> v = lab::shuffled(4096);
  for (auto _ : state) {
    long s = 0;
    for (int x : v) s += static_cast<long>(x) * x;
    benchmark::DoNotOptimize(s);
  }
}
BENCHMARK(BM_SumDoNotOptimize);

void BM_Sort(benchmark::State& state) {
  const std::vector<int> master = lab::shuffled(static_cast<std::size_t>(state.range(0)));
  std::vector<int> work;
  for (auto _ : state) {
    state.PauseTiming();  // the copy is O(N); keep it out of the fitted time
    work = master;
    state.ResumeTiming();
    std::sort(work.begin(), work.end());
    benchmark::ClobberMemory();
  }
  state.SetComplexityN(state.range(0));
}
BENCHMARK(BM_Sort)->RangeMultiplier(2)->Range(1 << 10, 1 << 16)->Complexity();

void BM_Series(benchmark::State& state) {
  long n = state.range(0);
  for (auto _ : state) {
    benchmark::DoNotOptimize(n);
    long s = 0;
    for (long i = 0; i < n; ++i) s += i;
    benchmark::DoNotOptimize(s);
  }
  state.SetComplexityN(state.range(0));
}
BENCHMARK(BM_Series)->RangeMultiplier(4)->Range(1 << 10, 1 << 20)->Complexity();

}  // namespace

BENCHMARK_MAIN();
