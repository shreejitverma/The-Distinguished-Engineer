#!/usr/bin/env bash
# argv-array.sh NAME [ARG...]: build a command line in an indexed array, then run it.
# Each element stays exactly one argument: spaces, glob characters and empty strings survive,
# because "${cmd[@]}" expands to one field per element with no field splitting or globbing.
# The command is printf '<%s>', so the output shows the exact argument boundaries.
# Example: argv-array.sh 'my name' 'a b.txt' '*' ''
#   -> <--name=my name><a b.txt><*><>
#   -> 6 words
cmd=(printf '<%s>')
cmd+=("--name=$1")
shift
for arg in "$@"; do
  cmd+=("$arg")
done
"${cmd[@]}"
printf '\n%d words\n' "${#cmd[@]}"
