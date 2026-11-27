#!/bin/sh
# path-values.sh [FILE...]: print every leaf as PATH=VALUE, path elements joined with ".".
# The join is lossy: {"a.b":1} and {"a":{"b":1}} both print a.b=1.
exec jq -r --stream '
  select(length == 2)
  | "\(.[0] | map(tostring) | join("."))=\(.[1] | tojson)"
' -- "$@"
