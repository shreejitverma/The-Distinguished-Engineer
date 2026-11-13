// Part 1 of the lab: a minimal microbenchmark harness rebuilt from first principles.
//
// It counts retired user-mode instructions with perf_event_open(2) instead of reading a clock,
// because an instruction count is nearly deterministic: interrupts and page faults run in kernel
// mode, which the counter excludes, so the lowest of a few readings is the work itself. Every
// line it prints is a derived fact (a class or a fitted curve), never a raw count, so the output
// is stable across runs on one machine and compiler.
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <limits>
#include <string_view>
#include <vector>

#include "fit.hpp"
#include "pmu.hpp"
#include "workloads.hpp"

namespace {

constexpr int kReps = 9;

// Lowest instruction count of kReps runs of op(), each preceded by an uncounted setup().
// Mirrors State::PauseTiming(): the counters are stopped while setup() runs.
template <class Setup, class Op>
std::uint64_t min_instructions(lab::PmuGroup& pmu, Setup setup, Op op) {
  std::uint64_t best = std::numeric_limits<std::uint64_t>::max();
  for (int r = 0; r < kReps; ++r) {
    setup();
    pmu.start();
    op();
    pmu.stop();
    const lab::Counts c = pmu.read_counts();
    if (c.instructions < best) best = c.instructions;
  }
  return best;
}

template <class Op>
std::uint64_t min_instructions(lab::PmuGroup& pmu, Op op) {
  return min_instructions(pmu, [] {}, op);
}

std::uint64_t g_overhead = 0;  // instructions of the measuring code around an empty call

double net(std::uint64_t raw) {
  return raw > g_overhead ? static_cast<double>(raw - g_overhead) : 0.0;
}

const char* classify(double instructions, double eliminated_below, double executed_from) {
  if (instructions < eliminated_below) return "eliminated";
  if (instructions >= executed_from) return "executed";
  return "partial";
}

int g_failures = 0;

void expect(bool ok, const char* what) {
  std::printf("  check %-58s %s\n", what, ok ? "ok" : "FAILED");
  if (!ok) ++g_failures;
}

// Fit a family measured at sizes ns and print the curve Google Benchmark's oAuto would choose.
lab::BigO report_fit(const char* name, const std::vector<double>& ns,
                     const std::vector<double>& counts) {
  const lab::Fit f = lab::fit_auto(ns, counts);
  std::printf("  %-34s best fit %-5s (%zu sizes, %.0f to %.0f)\n", name,
              lab::big_o_string(f.complexity), ns.size(), ns.front(), ns.back());
  return f.complexity;
}

void self_test() {
  std::printf("[1] fitter self-test (pure arithmetic, no counters)\n");
  std::vector<double> ns;
  for (int k = 8; k <= 16; ++k) ns.push_back(static_cast<double>(1u << k));
  int correct = 0;
  const lab::BigO all[] = {lab::BigO::o1,     lab::BigO::oLogN,     lab::BigO::oN,
                           lab::BigO::oNLogN, lab::BigO::oNSquared, lab::BigO::oNCubed};
  for (lab::BigO c : all) {
    std::vector<double> t;
    for (double n : ns) t.push_back(3.0 * lab::curve(c, n));
    if (lab::fit_auto(ns, t).complexity == c) ++correct;
  }
  std::printf("  exact curves classified correctly: %d/6\n", correct);
  expect(correct == 6, "every exact curve fits as itself");

  // The fit has no intercept: a constant term is charged to the growth term.
  std::vector<double> small;
  for (int k = 8; k <= 14; ++k) small.push_back(static_cast<double>(1u << k));
  auto trap = [&](const char* label, std::function<double(double)> f) {
    std::vector<double> t;
    for (double n : small) t.push_back(f(n));
    std::printf("  trap %-30s fits as %s\n", label,
                lab::big_o_string(lab::fit_auto(small, t).complexity));
  };
  trap("2N + 20000", [](double n) { return 2.0 * n + 20000.0; });
  trap("N lgN + 30N", [](double n) { return n * std::log2(n) + 30.0 * n; });
  std::vector<double> wide;
  for (int k = 8; k <= 20; k += 2) wide.push_back(static_cast<double>(1u << k));
  std::vector<double> t;
  for (double n : wide) t.push_back(100.0 + 5.0 * std::log2(n));
  std::printf("  trap %-30s fits as %s\n", "5 lgN + 100",
              lab::big_o_string(lab::fit_auto(wide, t).complexity));
}

void dead_code(lab::PmuGroup& pmu) {
  std::printf("[2] dead-code elimination, instructions per call (%s)\n", lab::compiler_family());
  const std::vector<int> v = lab::shuffled(4096);
  const double discarded = net(min_instructions(pmu, [&] { lab::sum_squares_discarded(v); }));
  const double sunk = net(min_instructions(pmu, [&] { lab::sum_squares_sunk(v); }));
  // 4096 elements: real work is at least ~0.25 instructions per element even fully vectorized.
  const char* d = classify(discarded, 64, 1024);
  const char* s = classify(sunk, 64, 1024);
  std::printf("  sum of squares, result discarded  : %s\n", d);
  std::printf("  sum of squares, DoNotOptimize(s)  : %s\n", s);
  expect(std::string_view(s) == "executed", "DoNotOptimize keeps the 4096-element loop");
  expect(sunk > 8.0 * (discarded + 1.0), "sunk variant retires 8x more instructions");

  const double plain = net(min_instructions(pmu, [] { lab::push_back_plain(); }));
  const double clobbered = net(min_instructions(pmu, [] { lab::push_back_clobbered(); }));
  std::printf("  push_back(42), no barrier         : %s\n", classify(plain, 16, 40));
  std::printf("  push_back(42), escape + clobber   : %s\n", classify(clobbered, 16, 40));
  expect(clobbered >= 40.0, "escape + ClobberMemory keeps new, store, delete");
}

void complexity(lab::PmuGroup& pmu) {
  std::printf("[3] complexity fitted on instruction counts\n");
  std::vector<double> ns, counts;

  for (int k = 10; k <= 16; ++k) {
    const long n = 1L << k;
    ns.push_back(static_cast<double>(n));
    counts.push_back(net(min_instructions(pmu, [n] { lab::run_series(n); })));
  }
  const lab::BigO series_fit = report_fit("series(n) loop, n escaped", ns, counts);

  ns.clear();
  counts.clear();
  for (int k = 10; k <= 16; ++k) {
    const std::vector<int> v = lab::shuffled(1u << k);
    ns.push_back(static_cast<double>(v.size()));
    counts.push_back(net(min_instructions(pmu, [&] { lab::run_find_absent(v); })));
  }
  expect(report_fit("std::find, key absent", ns, counts) == lab::BigO::oN, "std::find fits N");

  ns.clear();
  counts.clear();
  for (int k = 8; k <= 14; ++k) {
    const std::vector<int> master = lab::shuffled(1u << k);
    std::vector<int> work;
    ns.push_back(static_cast<double>(master.size()));
    counts.push_back(net(min_instructions(
        pmu, [&] { work = master; }, [&] { lab::run_sort(work); })));
  }
  expect(report_fit("std::sort, shuffled", ns, counts) == lab::BigO::oNLogN, "std::sort fits NlgN");

  ns.clear();
  counts.clear();
  for (int k = 8; k <= 22; k += 2) {
    std::vector<int> v = lab::shuffled(1u << k);
    std::sort(v.begin(), v.end());
    ns.push_back(static_cast<double>(v.size()));
    counts.push_back(net(min_instructions(pmu, [&] { lab::run_lower_bound(v); })));
  }
  expect(report_fit("std::lower_bound x256", ns, counts) == lab::BigO::oLogN,
         "std::lower_bound fits lgN");

  ns.clear();
  counts.clear();
  for (int k = 6; k <= 10; ++k) {
    const std::size_t n = 1u << k;
    std::vector<int> work;
    ns.push_back(static_cast<double>(n));
    counts.push_back(net(min_instructions(
        pmu,
        [&] {
          work.resize(n);
          for (std::size_t i = 0; i < n; ++i) work[i] = static_cast<int>(n - i);  // reversed
        },
        [&] { lab::run_insertion_sort(work); })));
  }
  expect(report_fit("insertion sort, reversed", ns, counts) == lab::BigO::oNSquared,
         "insertion sort fits N^2");

#if defined(__clang__)
  expect(series_fit == lab::BigO::o1, "clang rewrote the series loop to a closed form");
#else
  expect(series_fit == lab::BigO::oN, "this compiler kept the series loop");
#endif
}

}  // namespace

int main() {
  self_test();
  lab::pin_to_current_cpu();
  lab::PmuGroup pmu;
  g_overhead = min_instructions(pmu, [] { lab::empty_kernel(); });
  dead_code(pmu);
  complexity(pmu);
  std::printf("[4] counter group multiplexed during any reading: %s\n",
              pmu.multiplexed_reads() == 0 ? "no" : "yes");
  expect(pmu.multiplexed_reads() == 0, "every reading had the PMU for its whole window");
  return g_failures == 0 ? 0 : 1;
}
