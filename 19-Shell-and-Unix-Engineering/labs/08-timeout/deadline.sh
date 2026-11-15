#!/bin/sh
# deadline.sh: run a command under timeout(1) with a KILL backstop and say how it ended.
# Usage: deadline.sh SECONDS COMMAND [ARG...]
# Prints one line: ok, exit N, timed out, timeout failed, cannot invoke, not found, or signal N.
# Exits 0 for ok and 1 otherwise. TIMEOUT names the timeout binary (default: timeout on PATH).
T=${TIMEOUT:-timeout}
if [ "$#" -lt 2 ]; then
  echo 'usage: deadline.sh SECONDS COMMAND [ARG...]' >&2
  exit 2
fi
secs=$1
shift
# -k 2: if the command ignores the first signal, SIGKILL follows two seconds later.
"$T" -k 2 "$secs" "$@"
rc=$? # not "status": that name is read-only in zsh
case $rc in
  0) echo ok ;;
  124) echo 'timed out' ;;
  125) echo 'timeout failed' ;;
  126) echo 'cannot invoke' ;;
  127) echo 'not found' ;;
  *)
    # A shell reports death by signal N as 128+N, but ksh93 uses 256+N and yash 384+N.
    if [ "$rc" -gt 384 ]; then
      echo "signal $((rc - 384))"
    elif [ "$rc" -gt 256 ]; then
      echo "signal $((rc - 256))"
    elif [ "$rc" -gt 128 ]; then
      echo "signal $((rc - 128))"
    else
      echo "exit $rc"
    fi
    ;;
esac
[ "$rc" -eq 0 ]
