#!/usr/bin/env bash
# requires: linux, pmu
#
# Optimizer-proof microbenchmarks three ways: a harness rebuilt from perf_event_open(2), Google
# Benchmark, and nanobench. Every printed line is a derived fact, not a raw time or count.
# Host prerequisites besides a PMU: Google Benchmark (Ubuntu: apt install libbenchmark-dev, or set
# BENCHMARK_ROOT to an install prefix) and nanobench.h v4.4.0 or later (set NANOBENCH_INCLUDE to
# the directory holding it, default /usr/local/include or /usr/include).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C PYTHONDONTWRITEBYTECODE=1

CXX="${CXX:-c++}"
CXXFLAGS=(-std=c++17 -O2 -Wall -Wextra -I"${SCRIPT_DIR}")

if echo | "${CXX}" -dM -E -x c++ - | grep -q '__clang__'; then
    FAMILY=clang
else
    FAMILY=gcc
fi

GB_FLAGS=()
if [[ -n "${BENCHMARK_ROOT:-}" ]]; then
    GB_FLAGS=(-I"${BENCHMARK_ROOT}/include" -L"${BENCHMARK_ROOT}/lib" -Wl,-rpath,"${BENCHMARK_ROOT}/lib")
fi

NB_DIR="${NANOBENCH_INCLUDE:-}"
if [[ -z "${NB_DIR}" ]]; then
    for d in /usr/local/include /usr/include; do
        if [[ -f "${d}/nanobench.h" ]]; then NB_DIR="${d}"; break; fi
    done
fi
if [[ -z "${NB_DIR}" || ! -f "${NB_DIR}/nanobench.h" ]]; then
    echo "nanobench.h not found: set NANOBENCH_INCLUDE to the directory that holds it" >&2
    exit 1
fi

# Part 1: the rebuilt harness (fitter self-test, dead-code checks, counter-based complexity).
"${CXX}" "${CXXFLAGS[@]}" -o "${WORK}/rebuild" "${SCRIPT_DIR}/rebuild.cpp"
"${WORK}/rebuild"

# Part 2: Google Benchmark, time-based; the console table goes to /dev/null, the JSON to a file.
if ! "${CXX}" "${CXXFLAGS[@]}" "${GB_FLAGS[@]+"${GB_FLAGS[@]}"}" -o "${WORK}/gb_lab" \
        "${SCRIPT_DIR}/gb_lab.cpp" -lbenchmark -lpthread; then
    echo "Google Benchmark not found: install libbenchmark-dev or set BENCHMARK_ROOT" >&2
    exit 1
fi
"${WORK}/gb_lab" --benchmark_min_time=0.2s --benchmark_out="${WORK}/gb.json" \
    --benchmark_out_format=json >/dev/null 2>"${WORK}/gb.stderr"
python3 "${SCRIPT_DIR}/summarize_gb.py" "${WORK}/gb.json" "${FAMILY}"

# Part 3: nanobench with its built-in Linux counters.
"${CXX}" "${CXXFLAGS[@]}" -I"${NB_DIR}" -o "${WORK}/nb_lab" "${SCRIPT_DIR}/nb_lab.cpp"
"${WORK}/nb_lab"
