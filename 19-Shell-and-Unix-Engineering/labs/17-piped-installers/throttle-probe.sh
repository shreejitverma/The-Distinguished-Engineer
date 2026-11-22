#!/bin/sh
# throttle-probe.sh: show that a script which stalls makes its producer stall, the primitive behind
# server-side detection of `curl | sh`.
# Usage: throttle-probe.sh DIR PAD_BYTES SHELL [SHELL_ARG...]
# A producer writes a three-line script, then PAD_BYTES of 64-byte comment lines, then creates a
# marker file. The script sleeps for two seconds, reports whether the marker exists, and exits.
# If the padding does not fit in the pipe, the producer is still blocked in write(2) while the
# consumer sleeps, so the marker does not exist yet and the script prints "writer-blocked".
set -u
dir=$(mktemp -d "$1/throttle.XXXXXX") || exit 1
pad=$2
shift 2
done_marker=$dir/writer-done
export done_marker
{
  # shellcheck disable=SC2016 # the single-quoted lines are script text for the shell under test
  printf '%s\n' \
    '/bin/sleep 2' \
    'if [ -e "$done_marker" ]; then echo writer-finished; else echo writer-blocked; fi' \
    'exit 0'
  awk -v n="$pad" 'BEGIN {
    line = "#"
    for (j = 1; j < 63; j++) line = line "-"
    for (i = 0; i < n / 64; i++) print line
  }' 2>/dev/null
  : >"$done_marker"
} | "$@"
