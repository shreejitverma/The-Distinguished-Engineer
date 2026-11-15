#!/bin/sh
# leftmost.sh STRING ERE [AWK [ARG...]]: print where awk's match() says ERE matches STRING,
# as "RSTART RLENGTH TEXT", or "0 -1" when it does not match.
# The awk command defaults to awk; pass it as separate words (for example: busybox awk).
# The ERE travels through -v, so awk processes backslash escapes in it: keep it free of them.
set -eu

if [ "$#" -lt 2 ]; then
  echo "usage: leftmost.sh STRING ERE [AWK [ARG...]]" >&2
  exit 64
fi
string=$1
ere=$2
shift 2
if [ "$#" -eq 0 ]; then
  set -- awk
fi

"$@" -v s="$string" -v re="$ere" 'BEGIN {
  if (match(s, re)) print RSTART, RLENGTH, substr(s, RSTART, RLENGTH)
  else print RSTART, RLENGTH
}'
