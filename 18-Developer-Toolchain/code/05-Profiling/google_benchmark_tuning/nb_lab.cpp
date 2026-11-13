// Part 3 of the lab: the same experiments written against nanobench (v4.4.0 or later, for setup()).
//
// nanobench reads its own perf_event_open counters on Linux and subtracts a calibrated
// measuring overhead and loop overhead from the instruction count, so the median ins/op of a
// fully eliminated body is close to zero. Its built-in complexityBigO() fits the median time;
// here the same BigO class is fed the median instruction counts instead, which do not jitter.
#define ANKERL_NANOBENCH_IMPLEMENT
#include <nanobench.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "workloads.hpp"

namespace nb = ankerl::nanobench;

namespace {

int g_failures = 0;

void expect(bool ok, const char* what) {
  std::printf("  check %-58s %s\n", what, ok ? "ok" : "FAILED");
  if (!ok) ++g_failures;
}

double median_instructions(const nb::Bench& bench) {
  return bench.results().back().median(nb::Result::Measure::instructions);
}

bool has_instructions(const nb::Bench& bench) {
  return bench.results().back().has(nb::Result::Measure::instructions);
}

std::string best_instruction_fit(const nb::BigO::RangeMeasure& rm) {
  std::vector<nb::BigO> fits;
  fits.emplace_back("O(1)", rm, [](double) { return 1.0; });
  fits.emplace_back("O(n)", rm, [](double n) { return n; });
  fits.emplace_back("O(log n)", rm, [](double n) { return std::log2(n); });
  fits.emplace_back("O(n log n)", rm, [](double n) { return n * std::log2(n); });
  fits.emplace_back("O(n^2)", rm, [](double n) { return n * n; });
  fits.emplace_back("O(n^3)", rm, [](double n) { return n * n * n; });
  std::sort(fits.begin(), fits.end());  // ascending normalized RMS
  return fits.front().name();
}

}  // namespace

int main() {
  std::printf("[6] nanobench %d.%d.%d, instruction counters (%s)\n", ANKERL_NANOBENCH_VERSION_MAJOR,
              ANKERL_NANOBENCH_VERSION_MINOR, ANKERL_NANOBENCH_VERSION_PATCH,
              lab::compiler_family());
  const std::vector<int> v = lab::shuffled(4096);

  nb::Bench bench;
  bench.output(nullptr).epochs(11).minEpochIterations(100);

  bench.run("sum discarded", [&] { lab::sum_squares_discarded(v); });
  if (!has_instructions(bench)) {
    std::fprintf(stderr, "nanobench recorded no instruction counter: no PMU access\n");
    return 1;
  }
  const double discarded = median_instructions(bench);

  bench.run("sum doNotOptimizeAway", [&] {
    long s = 0;
    for (int x : v) s += static_cast<long>(x) * x;
    nb::doNotOptimizeAway(s);
  });
  const double sunk = median_instructions(bench);
  std::printf("  sum discarded, ins/op under 16        : %s\n", discarded < 16.0 ? "yes" : "no");
  std::printf("  doNotOptimizeAway, ins/op over 1024   : %s\n", sunk > 1024.0 ? "yes" : "no");
  expect(discarded < 16.0, "discarded reduction costs under 16 ins/op");
  expect(sunk > 1024.0, "doNotOptimizeAway keeps the 4096-element loop");

  // Sorting consumes its input, so each epoch is one iteration with an untimed setup() copy.
  nb::BigO::RangeMeasure sort_rm;
  for (int k = 8; k <= 14; ++k) {
    const std::vector<int> master = lab::shuffled(1u << k);
    std::vector<int> work;
    nb::Bench b;
    b.output(nullptr).epochs(9).epochIterations(1);
    b.setup([&] { work = master; }).run("sort", [&] { lab::run_sort(work); });
    sort_rm.emplace_back(static_cast<double>(master.size()), median_instructions(b));
  }
  const std::string sort_fit = best_instruction_fit(sort_rm);
  std::printf("  std::sort, fitted on median ins/op    : %s\n", sort_fit.c_str());
  expect(sort_fit == "O(n log n)", "nanobench BigO on instructions picks O(n log n)");
  return g_failures == 0 ? 0 : 1;
}
