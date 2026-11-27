#!/bin/sh
# gen-array.sh N [array|ndjson]: write N small records to stdout.
# Record i is {"id":i,"name":"user-i","tags":["a","b"],"score":S} where S is i % 100.
# "array" (the default) wraps the records in one top-level array; "ndjson" writes one per line.
set -eu
n=${1:?usage: gen-array.sh N [array|ndjson]}
mode=${2:-array}
case $mode in
  array | ndjson) ;;
  *)
    echo "gen-array.sh: mode must be array or ndjson, not $mode" >&2
    exit 2
    ;;
esac
awk -v n="$n" -v mode="$mode" 'BEGIN {
  open = (mode == "array") ? "[" : ""
  close_ = (mode == "array") ? "]\n" : ""
  sep = (mode == "array") ? "," : "\n"
  printf "%s", open
  for (i = 0; i < n; i++) {
    if (i > 0) printf "%s", sep
    printf "{\"id\":%d,\"name\":\"user-%d\",\"tags\":[\"a\",\"b\"],\"score\":%d}", i, i, i % 100
  }
  if (mode == "array") printf "%s", close_
  else if (n > 0) printf "\n"
}'
