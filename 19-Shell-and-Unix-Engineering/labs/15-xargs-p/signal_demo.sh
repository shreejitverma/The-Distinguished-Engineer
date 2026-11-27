#!/bin/sh
# signal_demo: send xargs a signal while it runs one-second jobs, and report what changed.
# Usage: signal_demo.sh grow|shrink|plain|lazy DIR
#   grow    xargs -P 1, 4 jobs, SIGUSR1 once the first job runs
#   shrink  xargs -P 3, 12 jobs, SIGUSR2 twice once three jobs run
#   plain   xargs with no -P, 2 jobs, SIGUSR1 once the first job runs
#   lazy    xargs -P 1, 3 three-second jobs, SIGUSR1 once the first job runs
# Prints: killed=yes|no (xargs died of the signal) peak=<max concurrent jobs>
#         late=<distinct concurrency seen by jobs 4..12>   (shrink only)
#         running=<jobs alive one second after the signal>   (lazy only)
# XARGS overrides the xargs command (default xargs).
mode=$1
dir=$2
here=$(cd "$(dirname "$0")" && pwd)
x=${XARGS:-xargs}
# bash 3.2 prints a job notice on stderr when a signalled background job is reaped; the demo reports
# its result on stdout, so drop stderr to keep the output exact.
exec 2>/dev/null

count_runs() {
  set -- "$dir"/run.*
  if [ -e "$1" ]; then echo "$#"; else echo 0; fi
}

wait_runs() {
  tries=100
  running=$(count_runs)
  while [ "$running" -lt "$1" ]; do
    tries=$((tries - 1))
    [ "$tries" -gt 0 ] || exit 3
    /bin/sleep 0.1
    running=$(count_runs)
  done
}

rm -f "$dir"/run.* "$dir/peak.log"
case $mode in
  grow) n=4 ;;
  shrink) n=12 ;;
  plain) n=2 ;;
  lazy) n=3 ;;
  *) exit 2 ;;
esac
seq 1 "$n" >"$dir/in"

case $mode in
  grow) "$x" -P 1 -n 1 "$here/peak_worker.sh" "$dir" 1 <"$dir/in" & ;;
  shrink) "$x" -P 3 -n 1 "$here/peak_worker.sh" "$dir" 1 <"$dir/in" & ;;
  plain) "$x" -n 1 "$here/peak_worker.sh" "$dir" 1 <"$dir/in" & ;;
  lazy) "$x" -P 1 -n 1 "$here/peak_worker.sh" "$dir" 3 <"$dir/in" & ;;
  *) exit 2 ;;
esac
pid=$!

case $mode in
  grow | plain)
    wait_runs 1
    kill -USR1 "$pid"
    ;;
  lazy)
    wait_runs 1
    kill -USR1 "$pid"
    /bin/sleep 1
    mid=$(count_runs)
    ;;
  shrink)
    wait_runs 3
    kill -USR2 "$pid"
    /bin/sleep 0.3
    kill -USR2 "$pid"
    ;;
  *) exit 2 ;;
esac
wait "$pid"
status=$?
if [ "$status" -gt 128 ]; then killed=yes; else killed=no; fi

# A killed xargs leaves its running jobs behind; let them finish so nothing outlives the demo.
tries=100
running=$(count_runs)
while [ "$running" -gt 0 ] && [ "$tries" -gt 0 ]; do
  tries=$((tries - 1))
  /bin/sleep 0.1
  running=$(count_runs)
done

peak=$(sort -n "$dir/peak.log" | tail -n 1)
if [ "$mode" = shrink ]; then
  late=$(tail -n +4 "$dir/peak.log" | sort -u | tr '\n' ',')
  printf 'killed=%s peak=%s late=%s\n' "$killed" "$peak" "$late"
elif [ "$mode" = lazy ]; then
  printf 'killed=%s peak=%s running=%s\n' "$killed" "$peak" "$mid"
else
  printf 'killed=%s peak=%s\n' "$killed" "$peak"
fi
