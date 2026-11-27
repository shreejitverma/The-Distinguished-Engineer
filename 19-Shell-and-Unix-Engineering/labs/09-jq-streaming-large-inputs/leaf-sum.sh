#!/bin/sh
# leaf-sum.sh FIELD [FILE...]: sum every number whose path ends in FIELD, in constant memory.
# FIELD is passed with --arg, never spliced into the program text, so any string is safe.
set -eu
field=${1:?usage: leaf-sum.sh FIELD [FILE...]}
shift
exec jq -n --stream --arg f "$field" '
  reduce (inputs | select(length == 2 and .[0][-1] == $f) | .[1]) as $v (0; . + $v)
' -- "$@"
