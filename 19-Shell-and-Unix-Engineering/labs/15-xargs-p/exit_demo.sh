#!/bin/sh
# exit_demo: run exit_worker.sh over ITEMs through "xargs -P JOBS -n 1" and report what happened.
# Usage: exit_demo.sh JOBS DIR ITEM...   (see exit_worker.sh for the items)
# Prints: status=<xargs exit status> seen=<sorted items that ran, comma separated> slow_done=yes|no
# slow_done=yes means the "slow" job had finished by the time xargs returned.
jobs=$1
dir=$2
shift 2
here=$(cd "$(dirname "$0")" && pwd)
x=${XARGS:-xargs}
rm -f "$dir/seen.log" "$dir/slow.done"
printf '%s\n' "$@" | "$x" -P "$jobs" -n 1 "$here/exit_worker.sh" "$dir" 2>/dev/null
status=$?
seen=$(sort "$dir/seen.log" | tr '\n' ',')
if [ -e "$dir/slow.done" ]; then done=yes; else done=no; fi
printf 'status=%s seen=%s slow_done=%s\n' "$status" "$seen" "$done"
