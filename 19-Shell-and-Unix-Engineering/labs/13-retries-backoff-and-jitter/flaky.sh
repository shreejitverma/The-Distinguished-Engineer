#!/bin/sh
# flaky.sh: a test double that fails the first FAILS calls, then succeeds.
# Usage: flaky.sh COUNTFILE FAILS STATUS
# Each call appends one line to COUNTFILE, so `wc -l <COUNTFILE` is the number of attempts.
# While the call number is at most FAILS it exits STATUS; STATUS INT means "kill myself with SIGINT".
countfile=$1 fails=$2 status=$3
echo call >>"$countfile"
calls=$(wc -l <"$countfile")
calls=$((calls))
if [ "$calls" -gt "$fails" ]; then
  exit 0
fi
if [ "$status" = INT ]; then
  kill -s INT "$$"
  /bin/sleep 1 # the signal normally lands first; never fall through to a success
  exit 99
fi
exit "$status"
