#!/bin/sh
# lines: count the lines of a file, or of standard input when the operand is "-".
# Usage: lines.sh [-n] FILE|-
#   -n  print the bare number instead of "<count> <name>"
# Exit status: 0 success, 2 unreadable input, 64 usage error (sysexits EX_USAGE).
usage() {
  printf 'usage: lines [-n] FILE|-\n' >&2
  exit 64
}

bare=
if [ "${1-}" = -n ]; then
  bare=1
  shift
fi
[ "$#" -eq 1 ] || usage

name=$1
if [ "$name" = - ]; then
  count=$(wc -l | tr -d ' ')
elif [ -r "$name" ]; then
  count=$(wc -l <"$name" | tr -d ' ')
else
  printf 'lines: cannot read: %s\n' "$name" >&2
  exit 2
fi

if [ -n "$bare" ]; then
  printf '%s\n' "$count"
else
  printf '%s %s\n' "$count" "$name"
fi
