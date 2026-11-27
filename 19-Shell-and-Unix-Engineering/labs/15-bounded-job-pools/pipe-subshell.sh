#!/bin/sh
# pipe-subshell.sh EVENTS: start a 1 s worker from the last stage of a pipeline, then run a bare "wait"
# in the parent. If that stage ran in a subshell, the parent knows no children and wait returns at once.
# Prints "waited=yes" if the worker had finished when wait returned, "waited=no" if it was still running.
here=${0%/*}
printf '%s\n' x | while read -r _line; do
  "$here/worker.sh" "$1" 1 1 &
done
wait
if grep -q '^- 1$' "$1"; then
  echo waited=yes
else
  echo waited=no
fi
/bin/sleep 2 # let the worker finish before the caller removes its files
