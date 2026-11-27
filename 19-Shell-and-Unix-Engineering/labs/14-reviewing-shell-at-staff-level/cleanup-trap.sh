#!/bin/sh
# cleanup-trap.sh MODE: create a scratch directory under $TMPDIR, print its path, then end in a
# chosen way, so a test can check whether the cleanup ran.
# MODE: leak (no trap), normal, fail, term (TERM converted to exit), kill (SIGKILL, untrappable).
mode=${1:?usage: cleanup-trap.sh MODE}
base=${TMPDIR:-/tmp}
work=$(mktemp -d "$base/work.XXXXXX") || exit 1
printf '%s\n' "$work"
case $mode in
  leak | normal | fail | term | kill) ;;
  *)
    rmdir "$work"
    echo "cleanup-trap.sh: unknown mode: $mode" >&2
    exit 64
    ;;
esac
# The EXIT trap runs on exit, on an exit from set -e, and on an exit called from another trap.
if [ "$mode" != leak ]; then
  trap 'rm -rf "$work"' EXIT
fi
case $mode in
  normal) exit 0 ;;
  fail)
    set -e
    false
    ;;
  term)
    trap 'exit 143' TERM
    kill -TERM $$
    ;;
  kill) kill -KILL $$ ;;
  leak) exit 0 ;;
  *) exit 64 ;;
esac
