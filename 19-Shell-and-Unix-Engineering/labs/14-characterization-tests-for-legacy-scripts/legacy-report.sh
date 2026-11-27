#!/bin/sh
# legacy-report.sh - the script under characterization (lab fixture, deliberately legacy-shaped).
# Summarizes the line counts of the *.log files in a directory into ./report.txt and mails an
# alert when the total exceeds THRESHOLD (default 10). Known quirks, kept on purpose:
#   - it splits `ls` output at blanks, so a file name with a blank is miscounted as skipped files;
#   - it leaves $TMPDIR/report.PID.tmp behind on every run;
#   - it stamps the report with the clock and its own pid, so the output is never repeatable.
# Usage: legacy-report.sh DIR

THRESHOLD=${THRESHOLD:-10}

notify() {
  printf 'total=%s\n' "$1" | mail -s "log alert" ops@example.invalid
}

summarize() {
  total=0
  skipped=0
  # shellcheck disable=SC2045,SC2086,SC2248 # legacy quirk under test: names are split at blanks
  for f in $(ls "$1"/*.log 2>/dev/null); do
    if [ ! -f "$f" ]; then
      skipped=$((skipped + 1))
      continue
    fi
    n=$(wc -l <"$f" | tr -d ' ')
    echo "$f $n"
    total=$((total + n))
  done
  echo "total $total"
  echo "skipped $skipped"
}

main() {
  if [ "$#" -lt 1 ]; then
    echo "usage: legacy-report.sh DIR" >&2
    exit 2
  fi
  if [ ! -d "$1" ]; then
    echo "no such directory: $1" >&2
    exit 1
  fi
  tmp=${TMPDIR:-/tmp}/report.$$.tmp
  summarize "$1" >"$tmp"
  stamp=$(date '+%Y-%m-%d %H:%M:%S')
  {
    echo "report generated $stamp by pid $$"
    cat "$tmp"
  } >report.txt
  total=$(sed -n 's/^total //p' "$tmp")
  if [ "$total" -gt "$THRESHOLD" ]; then
    notify "$total"
  fi
  cat report.txt
}

main "$@"
