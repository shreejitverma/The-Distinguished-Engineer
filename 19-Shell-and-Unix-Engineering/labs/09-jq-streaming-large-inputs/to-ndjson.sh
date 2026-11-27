#!/bin/sh
# to-ndjson.sh [FILE...]: the streaming equivalent of `jq -c '.[]'`.
# Emits each element of every top-level array, and each member value of every top-level object,
# as one compact line, holding one element at a time instead of the whole document.
# The stock `fromstream(1|truncate_stream(inputs))` drops elements that are scalars or empty containers (a leaf at
# depth 1 has a path of length 1, which truncate_stream(1) discards); the depth-1 branch here
# rewrites such a leaf into a top-level value so fromstream emits it.
# -n is required: without it jq consumes the first event as `.` and `inputs` never sees it.
exec jq -cn --stream '
  fromstream(
    inputs
    | (.[0] | length) as $d
    | if $d == 0 then empty
      elif $d == 1 then (if length == 2 then [[], .[1]] else empty end)
      else [.[0][1:]] + .[1:]
      end
  )
' -- "$@"
