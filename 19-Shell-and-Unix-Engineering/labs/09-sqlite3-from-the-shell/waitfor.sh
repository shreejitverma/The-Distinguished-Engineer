#!/bin/sh
# waitfor.sh FILE [SECONDS]: poll once a second until FILE exists; exit 1 after SECONDS (default 30).
# The lab uses marker files to order two sqlite3 processes without timing guesses.
n=0
limit=${2:-30}
while [ ! -e "$1" ]; do
  n=$((n + 1))
  if [ "$n" -gt "$limit" ]; then
    exit 1
  fi
  /bin/sleep 1
done
