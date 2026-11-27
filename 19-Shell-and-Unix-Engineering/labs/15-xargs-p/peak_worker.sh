#!/bin/sh
# peak_worker: a job that records how many peers are alive at the same time.
# Usage: peak_worker.sh DIR SECONDS [ITEM...]
# It creates DIR/run.PID, appends the number of DIR/run.* files it can see to DIR/peak.log,
# sleeps SECONDS in a real /bin/sleep, then removes its marker.
# A line in peak.log can only be too small, never too big, so the maximum is a lower bound on the
# real concurrency and is exact when every job outlives the start-up of all the others.
dir=$1
secs=$2
: >"$dir/run.$$"
set -- "$dir"/run.*
printf '%d\n' "$#" >>"$dir/peak.log"
/bin/sleep "$secs"
rm -f "$dir/run.$$"
