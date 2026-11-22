#!/bin/sh
# truncate-sweep.sh: feed every possible truncation of an installer to a shell through a pipe.
# Usage: truncate-sweep.sh FIXTURE SHELL [SHELL_ARG...]
# For each cut length N from 1 to size-1 (BSD head rejects -c 0, and an empty script runs nothing) it runs `head -c N FIXTURE | SHELL ...` and prints
# "N<TAB>TARGET" where TARGET is the path the stand-in remove() received, or "-" if it never ran.
# Cutting at size N means the connection dropped after N bytes.
set -u
fixture=$1
shift
size=$(wc -c <"$fixture")
size=${size##* }
nl='
'
n=1
while [ "$n" -lt "$size" ]; do
  out=$(head -c "$n" "$fixture" | "$@" 2>/dev/null)
  case $out in
    *"REMOVE "*)
      target=${out#*"REMOVE "}
      target=${target%%"$nl"*}
      ;;
    *)
      target=-
      ;;
  esac
  printf '%s\t%s\n' "$n" "$target"
  n=$((n + 1))
done
