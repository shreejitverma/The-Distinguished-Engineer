// Optimizer barriers rebuilt from first principles, and the kernels every part of the lab measures.
//
// do_not_optimize is the Clang (and Intel) branch of benchmark::DoNotOptimize(Tp&) in Google
// Benchmark v1.10.0 include/benchmark/utils.h, used here for GCC too (GCC 5+ gets a separate branch
// there that picks "+m" for large or non-trivial types): an empty asm statement that claims to read and write `value`
// ("+r,m": in a register or in memory, compiler's choice) and to touch all memory ("memory").
// clobber_memory is the classic form of benchmark::ClobberMemory(); v1.10.0 spells it
// std::atomic_signal_fence(std::memory_order_acq_rel), a compiler-only fence with the same effect.
#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace lab {

#define LAB_INLINE inline __attribute__((always_inline))
#define LAB_NOINLINE __attribute__((noinline))

template <class T>
LAB_INLINE void do_not_optimize(T& value) {
#if defined(__clang__)
  asm volatile("" : "+r,m"(value) : : "memory");
#else
  asm volatile("" : "+m,r"(value) : : "memory");
#endif
}

LAB_INLINE void clobber_memory() { asm volatile("" : : : "memory"); }

inline const char* compiler_family() {
#if defined(__clang__)
  return "clang";
#elif defined(__GNUC__)
  return "gcc";
#else
  return "other";
#endif
}

// Deterministic input: xorshift32, so every run and every compiler sees the same data.
inline std::vector<int> shuffled(std::size_t n, std::uint32_t seed = 2463534242u) {
  std::vector<int> v(n);
  for (std::size_t i = 0; i < n; ++i) {
    seed ^= seed << 13;
    seed ^= seed >> 17;
    seed ^= seed << 5;
    v[i] = static_cast<int>(seed & 0x7fffffff);
  }
  return v;
}

// --- Experiment 1: a reduction whose result is never used ----------------------------------

LAB_NOINLINE inline void sum_squares_discarded(const std::vector<int>& v) {
  long s = 0;
  for (int x : v) s += static_cast<long>(x) * x;
  (void)s;  // dead: nothing observable depends on s
}

LAB_NOINLINE inline void sum_squares_sunk(const std::vector<int>& v) {
  long s = 0;
  for (int x : v) s += static_cast<long>(x) * x;
  do_not_optimize(s);  // s must exist in a register or memory here
}

// --- Experiment 2: a store into memory nobody reads (the user guide's push_back example) ------

LAB_NOINLINE inline void push_back_plain() {
  std::vector<int> v;
  v.reserve(1);
  v.push_back(42);  // new, store, delete: no observable effect
}

LAB_NOINLINE inline void push_back_clobbered() {
  std::vector<int> v;
  v.reserve(1);
  int* data = v.data();
  do_not_optimize(data);  // the buffer address escapes
  v.push_back(42);
  clobber_memory();  // the store must reach the escaped buffer
}

// --- Complexity kernels -----------------------------------------------------------------------

// Arithmetic series with a runtime bound. Clang's induction-variable simplification replaces the
// loop with a closed form, so the "O(N)" benchmark is O(1) under Clang; GCC 13 keeps the loop.
LAB_NOINLINE inline long series(long n) {
  long s = 0;
  for (long i = 0; i < n; ++i) s += i;
  return s;
}

LAB_NOINLINE inline void run_series(long n) {
  do_not_optimize(n);  // the bound is unknown to the optimizer
  long s = series(n);
  do_not_optimize(s);
}

LAB_NOINLINE inline void run_find_absent(const std::vector<int>& v) {
  int key = -1;  // inputs are non-negative, so the whole vector is scanned
  do_not_optimize(key);
  auto it = std::find(v.begin(), v.end(), key);
  do_not_optimize(it);
}

LAB_NOINLINE inline void run_sort(std::vector<int>& v) {
  std::sort(v.begin(), v.end());
  clobber_memory();
}

constexpr int kLookupsPerCall = 256;

LAB_NOINLINE inline void run_lower_bound(const std::vector<int>& sorted) {
  std::uint32_t key = 12345u;
  long acc = 0;
  for (int i = 0; i < kLookupsPerCall; ++i) {
    key = key * 1664525u + 1013904223u;  // LCG keys, spread over the value range
    auto it = std::lower_bound(sorted.begin(), sorted.end(), static_cast<int>(key >> 1));
    acc += it - sorted.begin();
  }
  do_not_optimize(acc);
}

LAB_NOINLINE inline void run_insertion_sort(std::vector<int>& v) {
  for (std::size_t i = 1; i < v.size(); ++i) {
    const int x = v[i];
    std::size_t j = i;
    while (j > 0 && v[j - 1] > x) {
      v[j] = v[j - 1];
      --j;
    }
    v[j] = x;
  }
  clobber_memory();
}

LAB_NOINLINE inline void empty_kernel() {}

}  // namespace lab
