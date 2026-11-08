#!/usr/bin/env bash
set -euo pipefail

# The harness has no requirement token for a Bash version, so fail loudly instead of printing a wrong recording.
# Here-documents use pipes from Bash 5.1; the macOS /bin/bash (3.2) has no lastpipe and always uses temporary files.
if [ "${BASH_VERSINFO[0]}" -lt 5 ] || { [ "${BASH_VERSINFO[0]}" -eq 5 ] && [ "${BASH_VERSINFO[1]}" -lt 1 ]; }; then
    echo "pipeline_redirection lab needs Bash 5.1 or later first on PATH, found ${BASH_VERSION}" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

TMPDIR_LAB="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_LAB}" "${SCRIPT_DIR}/pipeline_engine"' EXIT

export LC_ALL=C

# Compile the C kernel mechanism verification engine
"${CC:-cc}" -Wall -Wextra -O2 "${SCRIPT_DIR}/pipeline_engine.c" -o "${SCRIPT_DIR}/pipeline_engine"

# Section 1: Low-level kernel mechanics (dup2, offset sharing, pipe EOF)
"${SCRIPT_DIR}/pipeline_engine" dup2_cloexec
"${SCRIPT_DIR}/pipeline_engine" offset_sharing "${TMPDIR_LAB}"
"${SCRIPT_DIR}/pipeline_engine" unclosed_write_end

# Section 2: Shell pipeline, redirection, subshell, and substitution semantics
bash "${SCRIPT_DIR}/pipeline_test.sh"
