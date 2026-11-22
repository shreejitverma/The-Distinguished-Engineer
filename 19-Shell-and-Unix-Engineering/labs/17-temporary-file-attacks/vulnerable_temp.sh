#!/usr/bin/env sh
# Predictable temporary file anti-pattern using process ID ($$)
set -eu

work_dir="${1:-.}"
target_name="${2:-job_$$}"
tmpfile="${work_dir}/${target_name}.tmp"

# Simulates writing sensitive or state data via redirection
printf '%s\n' "CRITICAL_PAYLOAD_DATA" >"$tmpfile"
printf 'WROTE: %s\n' "$tmpfile"
