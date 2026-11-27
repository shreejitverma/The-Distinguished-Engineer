#!/bin/sh
# set-e-wait.sh: under "set -e", a failing "wait PID" ends the script at the first failed job while the
# other workers are still running. The EXIT trap reports (and kills) the worker that was left behind.
# Prints "reached" only if the script survived the wait.
set -e
here=${0%/*}
"$here/worker.sh" /dev/null 1 0 3 &
failing=$!
"$here/worker.sh" /dev/null 2 hold &
slow=$!
cleanup() {
  if kill -0 "$slow" 2>/dev/null; then
    echo "slow worker still running at exit"
    kill "$slow"
  fi
}
trap cleanup EXIT
wait "$failing"
echo reached
