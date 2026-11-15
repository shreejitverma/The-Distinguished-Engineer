#!/bin/sh
# wce-loop: run a command up to MAX times in the foreground, like a build
# driver or a for-each-host loop, and report how far it got.
# Usage: wce-loop.sh MAX CMD [ARG...]
# A shell that waits for its foreground child and then decides whether a
# SIGINT it also received should end the loop (Cracauer's "wait and
# cooperative exit") keeps going when the child exits 130 and stops when the
# child dies of SIGINT.

max=${1:?max}
shift
i=0
while [ "$i" -lt "$max" ]; do
  i=$((i + 1))
  printf 'iteration %s\n' "$i"
  "$@"
done
printf 'loop-finished\n'
