#!/bin/sh
# term_demo: kill xargs with SIGTERM while two jobs run, and report whether the jobs survive.
# Usage: term_demo.sh DIR
# Prints: xargs_killed=yes|no jobs_survived=yes|no
# jobs_survived=yes means both one-second "slow" jobs were still running after xargs died
# and then finished on their own.
dir=$1
here=$(cd "$(dirname "$0")" && pwd)
x=${XARGS:-xargs}
# bash 3.2 prints a job notice on stderr when a signalled background job is reaped; the demo reports
# its result on stdout, so drop stderr to keep the output exact.
exec 2>/dev/null

seen_count() {
  if [ -f "$dir/seen.log" ]; then
    lines=$(wc -l <"$dir/seen.log")
    echo "$((lines))"
  else
    echo 0
  fi
}

rm -f "$dir/seen.log" "$dir/slow.done"
printf '%s\n' slow slow >"$dir/in"
"$x" -P 2 -n 1 "$here/exit_worker.sh" "$dir" <"$dir/in" &
pid=$!
tries=100
seen=$(seen_count)
while [ "$seen" -lt 2 ]; do
  tries=$((tries - 1))
  [ "$tries" -gt 0 ] || exit 3
  /bin/sleep 0.1
  seen=$(seen_count)
done
kill -TERM "$pid"
# Some shells (bash 3.2) print a "Terminated" notice when they reap the job; keep stderr clean.
{
  wait "$pid"
  status=$?
} 2>/dev/null
if [ "$status" -gt 128 ]; then killed=yes; else killed=no; fi
survived=no
if [ ! -e "$dir/slow.done" ]; then
  tries=100
  while [ ! -e "$dir/slow.done" ] && [ "$tries" -gt 0 ]; do
    tries=$((tries - 1))
    /bin/sleep 0.1
  done
  if [ -e "$dir/slow.done" ]; then survived=yes; fi
fi
printf 'xargs_killed=%s jobs_survived=%s\n' "$killed" "$survived"
