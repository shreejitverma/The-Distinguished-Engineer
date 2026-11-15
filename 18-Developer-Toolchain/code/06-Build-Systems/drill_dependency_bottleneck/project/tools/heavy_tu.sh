#!/usr/bin/env bash
# Compiler launcher (CMAKE_CXX_COMPILER_LAUNCHER): wait DRILL_TU_SECONDS (default 1), then run
# the real compile. The sleep stands in for a heavy translation unit, so every TU costs about
# the same wall time on any host and the schedule, not the CPU, decides the build time.
# sleep uses no CPU, so the shape of the schedule does not depend on the core count.
set -euo pipefail
sleep "${DRILL_TU_SECONDS:-1}"
exec "$@"
