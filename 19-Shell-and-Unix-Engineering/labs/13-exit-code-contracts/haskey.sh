#!/bin/sh
# haskey.sh: report whether KEY is defined in a KEY=VALUE file.
# Exit status contract (documented here, tested by the lab, never reused):
#   0  KEY is defined
#   1  KEY is not defined (a definite "no", not a failure)
#   2  usage error: wrong argument count, or KEY is not a valid name
#   3  FILE could not be read, so the question has no answer
# Usage: haskey.sh KEY FILE
usage() {
  printf 'usage: haskey.sh KEY FILE\n' >&2
  exit 2
}
[ "$#" -eq 2 ] || usage
key=$1
file=$2
case $key in
  '' | [0-9]* | *[!A-Za-z0-9_]*) usage ;;
  *) ;;
esac
# grep has three outcomes (0 match, 1 no match, >1 error); keep all three.
rc=0
grep -q -e "^$key=" -- "$file" 2>/dev/null || rc=$?
case $rc in
  0) exit 0 ;;
  1) exit 1 ;;
  *)
    printf 'haskey.sh: cannot read %s (grep status %s)\n' "$file" "$rc" >&2
    exit 3
    ;;
esac
