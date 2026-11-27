#!/bin/sh
# line_writer: print 100 lines "w<ID> xxxx..." (100 bytes each, newline included) to stdout.
# Usage: line_writer.sh ID
pad=$(printf '%96s' '' | tr ' ' x)
i=0
while [ "$i" -lt 100 ]; do
  printf 'w%s %s\n' "$1" "$pad"
  i=$((i + 1))
done
