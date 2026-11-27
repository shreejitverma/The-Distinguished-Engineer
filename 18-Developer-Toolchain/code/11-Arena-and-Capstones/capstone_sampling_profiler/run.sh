#!/usr/bin/env bash
# requires: linux
set -euo pipefail

CXX="${CXX:-g++}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Build workload and profiler with frame pointers preserved
"${CXX}" -std=c++17 -O2 -g \
    -fno-omit-frame-pointer \
    -mno-omit-leaf-frame-pointer \
    workload.cpp elf_symbolizer.cpp sampler.cpp main.cpp \
    -o capstone_profiler \
    -lpthread -lrt

# Run profiler and pipe folded stack output to verification script
./capstone_profiler 240 | python3 verify_profile.py

# Clean up build artifacts
rm -f capstone_profiler
