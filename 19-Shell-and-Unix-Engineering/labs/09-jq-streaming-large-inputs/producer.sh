#!/bin/sh
# producer.sh MODE MARKER N: write N small records to stdout, then create the file MARKER.
# MODE ndjson: N lines {"a":i}, each a complete top-level value.
# MODE open-array: "[" then N elements {"a":i} separated by commas, and never the closing "]".
# The marker appears only if every byte was written, so a consumer that exits early (the pipe
# breaks and this process dies of SIGPIPE or a write error) leaves it absent.
set -eu
mode=${1:?usage: producer.sh ndjson|open-array MARKER N}
marker=${2:?usage: producer.sh ndjson|open-array MARKER N}
n=${3:?usage: producer.sh ndjson|open-array MARKER N}
case $mode in
  ndjson | open-array) ;;
  *)
    echo "producer.sh: mode must be ndjson or open-array, not $mode" >&2
    exit 2
    ;;
esac
exec awk -v mode="$mode" -v m="$marker" -v n="$n" 'BEGIN {
  if (mode == "open-array") printf "["
  for (i = 0; i < n; i++) {
    if (mode == "open-array") printf "%s{\"a\":%d}", (i > 0 ? "," : ""), i
    else print "{\"a\":" i "}"
  }
  printf "" > m
  close(m)
}'
