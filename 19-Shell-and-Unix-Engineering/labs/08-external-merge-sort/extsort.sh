#!/bin/sh
# extsort.sh: a portable external merge sort built from awk, sort, and sort -m.
#
# Usage: extsort.sh [-o OUTPUT] RUN_LINES FANIN [FILE...]
#
# Phase 1 cuts the input into runs of RUN_LINES lines and sorts each run.
# Phase 2 merges at most FANIN runs at a time until one run is left.
# With -o, the result goes to OUTPUT and "runs=R passes=P" goes to stdout;
# without -o, the result goes to stdout and nothing else is printed.
# Ordering is byte order (LC_ALL=C) in every phase, so runs and merges agree.
set -eu

usage() {
  printf 'usage: extsort.sh [-o OUTPUT] RUN_LINES FANIN [FILE...]\n' >&2
  exit 2
}

output=
if [ "${1-}" = -o ]; then
  [ "$#" -ge 2 ] || usage
  output=$2
  shift 2
fi
[ "$#" -ge 2 ] || usage
run_lines=$1
fanin=$2
shift 2
case $run_lines in
  '' | *[!0-9]* | 0) usage ;;
  *) ;;
esac
case $fanin in
  '' | *[!0-9]* | 0 | 1) usage ;;
  *) ;;
esac

LC_ALL=C
export LC_ALL

work=$(mktemp -d "${TMPDIR:-/tmp}/extsort.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Phase 1: run formation. awk writes r0.1, r0.2, ... and prints the run count.
nruns=$(awk -v n="$run_lines" -v dir="$work" '
  (NR - 1) % n == 0 { if (f != "") close(f); f = dir "/r0." (++k) }
  { print > f }
  END { print k + 0 }' ${1+"$@"})
i=1
while [ "$i" -le "$nruns" ]; do
  sort -o "$work/r0.$i" "$work/r0.$i"
  i=$((i + 1))
done

# Phase 2: merge passes. Pass p reads r<p>.1..n and writes r<p+1>.1..out.
pass=0
n=$nruns
while [ "$n" -gt "$fanin" ]; do
  next=$((pass + 1))
  j=1
  out=0
  while [ "$j" -le "$n" ]; do
    set --
    while [ "$#" -lt "$fanin" ] && [ "$j" -le "$n" ]; do
      set -- ${1+"$@"} "$work/r$pass.$j"
      j=$((j + 1))
    done
    out=$((out + 1))
    sort -m -o "$work/r$next.$out" "$@"
    rm -f "$@"
  done
  pass=$next
  n=$out
done

# Final pass: merge the last n <= FANIN runs straight into the destination.
set --
j=1
while [ "$j" -le "$n" ]; do
  set -- ${1+"$@"} "$work/r$pass.$j"
  j=$((j + 1))
done
passes=$pass
if [ "$n" -gt 1 ]; then
  passes=$((pass + 1))
fi
if [ -n "$output" ]; then
  if [ "$n" -eq 0 ]; then
    : >"$output"
  else
    sort -m -o "$output" "$@"
  fi
  printf 'runs=%s passes=%s\n' "$nruns" "$passes"
elif [ "$n" -gt 0 ]; then
  sort -m "$@"
fi
