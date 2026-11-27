#!/usr/bin/env bash
# requires: linux
set -euo pipefail

# Production Incident Simulation Drills - Master Lab Runner
# Verifies all six reproduction scenarios in deterministic sequence.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export CC="${CC:-cc}"
export CXX="${CXX:-c++}"
export PYTHONUNBUFFERED=1

echo "=================================================================="
echo "RUNNING DRILL 1: REMOTE BUILD CACHE POISONING VERIFICATION"
echo "=================================================================="
python3 "${SCRIPT_DIR}/drill1_cache_poison.py"

echo "=================================================================="
echo "RUNNING DRILL 2: SHARED-LIBRARY ABI DRIFT & MEMORY CORRUPTION"
echo "=================================================================="
bash "${SCRIPT_DIR}/drill2_abi_drift.sh"

echo "=================================================================="
echo "RUNNING DRILL 3: COMPROMISED DEPENDENCY CREDENTIAL EXFILTRATION"
echo "=================================================================="
python3 "${SCRIPT_DIR}/drill3_exfiltration.py"

echo "=================================================================="
echo "RUNNING DRILL 4: ASYNC RUNTIME THREAD POOL STARVATION"
echo "=================================================================="
python3 "${SCRIPT_DIR}/drill4_async_starvation.py"

echo "=================================================================="
echo "RUNNING DRILL 5: DYNAMIC LINKER RPATH VS RUNPATH INTERPOSITION"
echo "=================================================================="
bash "${SCRIPT_DIR}/drill5_rpath_runpath.sh"

echo "=================================================================="
echo "RUNNING DRILL 6: CORRUPTED GIT OBJECT RESCUE UNDER P0 EMERGENCY"
echo "=================================================================="
python3 "${SCRIPT_DIR}/drill6_git_corrupt.py"

echo "=================================================================="
echo "ALL SIX DRILL VERIFICATIONS COMPLETED SUCCESSFULLY"
echo "=================================================================="
