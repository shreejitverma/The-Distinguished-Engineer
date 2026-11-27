#!/bin/sh
# run_pool: push N one-item jobs through "xargs -P JOBS -n 1" and report the observed peak.
# Usage: run_pool.sh JOBS N SECONDS DIR      (JOBS "default" omits -P)
# Prints: peak=<max concurrent jobs> invocations=<jobs run> status=<xargs exit status>
# The worker is peak_worker.sh; XARGS overrides the xargs command (default xargs).
jobs=$1
n=$2
secs=$3
dir=$4
here=$(cd "$(dirname "$0")" && pwd)
x=${XARGS:-xargs}
rm -f "$dir/peak.log" "$dir"/run.*
if [ "$jobs" = default ]; then
  set -- -n 1
else
  set -- -P "$jobs" -n 1
fi
seq 1 "$n" | "$x" "$@" "$here/peak_worker.sh" "$dir" "$secs"
status=$?
peak=$(sort -n "$dir/peak.log" | tail -n 1)
count=$(wc -l <"$dir/peak.log")
printf 'peak=%s invocations=%s status=%s\n' "$peak" "$((count))" "$status"
