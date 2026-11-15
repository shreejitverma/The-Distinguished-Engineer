#!/bin/sh
# tools.sh BOUNDED N: run this system's grep -E and sed -E on two nested-quantifier patterns
# against one line of N bytes of "a" followed by "!", each run bounded to 5 seconds by BOUNDED
# (the compiled bounded.c, which exits 124 on timeout). A backtracking engine needs time
# exponential in N here; an automaton-based one needs time linear in N.
set -u
bounded=${1:?usage: tools.sh BOUNDED N}
n=${2:?usage: tools.sh BOUNDED N}

dir=$(mktemp -d) || exit 2
trap 'rm -rf "$dir"' EXIT
awk -v n="$n" 'BEGIN { s = ""; for (i = 0; i < n; i++) s = s "a"; print s "!" }' >"$dir/line"

for re in '(a|aa)*c' '^(a+)+$'; do
  "$bounded" 5 grep -E "$re" "$dir/line" >"$dir/out"
  status=$?
  lines=$(wc -l <"$dir/out" | tr -d ' ')
  printf 'grep -E %s: status %s, %s lines\n' "$re" "$status" "$lines"
  "$bounded" 5 sed -n -E "/$re/p" "$dir/line" >"$dir/out"
  status=$?
  lines=$(wc -l <"$dir/out" | tr -d ' ')
  printf 'sed -E %s: status %s, %s lines\n' "$re" "$status" "$lines"
done
