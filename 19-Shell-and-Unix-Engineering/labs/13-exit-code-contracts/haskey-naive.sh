#!/bin/sh
# haskey-naive.sh: the same question in the usual if-grep shape.
# It collapses grep's three outcomes into two, so a file that cannot be
# read is reported as "KEY is not defined" (status 1) instead of an error.
# Usage: haskey-naive.sh KEY FILE
if grep -q -e "^$1=" -- "$2" 2>/dev/null; then
  exit 0
else
  exit 1
fi
