#!/bin/sh
# bare-wait.sh MODE: start one worker that runs 1 s and exits 3, then ask for its status.
#   direct: wait for the PID; prints "status=3".
#   bare:   run a bare "wait" first, then ask for the PID; prints the status the shell still remembers,
#           which is 127 where a bare wait forgets a job that was still running when it started.
here=${0%/*}
"$here/worker.sh" /dev/null 1 1 3 &
pid=$!
case $1 in
  direct) ;;
  bare) wait ;;
  *)
    echo "usage: bare-wait.sh direct|bare" >&2
    exit 2
    ;;
esac
wait "$pid"
echo "status=$?"
