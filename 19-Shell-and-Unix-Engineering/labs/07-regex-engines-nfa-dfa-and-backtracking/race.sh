#!/bin/sh
# race.sh ENGINES: steps taken by three matchers for (a+)+b against n bytes of "a" (no "b"),
# then one backtracking run stopped by a step limit, as PCRE2's match limit stops pcre2_match.
# ENGINES is the compiled engines.c.
set -u
engines=${1:?usage: race.sh ENGINES}

# a_line N: print N bytes of "a" and a newline.
a_line() {
  awk -v n="$1" 'BEGIN { s = ""; for (i = 0; i < n; i++) s = s "a"; print s }'
}

# steps MODE N: print the step count one engine reports for the n-byte subject.
steps() {
  out=$(a_line "$2" | "$engines" "$1" '(a+)+b') || :
  out=${out##*steps=}
  printf '%s\n' "${out%% *}"
}

printf '%3s %10s %5s %8s\n' n backtrack memo thompson
for n in 4 8 12 16 20; do
  bt=$(steps backtrack "$n")
  memo=$(steps memo "$n")
  nfa=$(steps thompson "$n")
  printf '%3s %10s %5s %8s\n' "$n" "$bt" "$memo" "$nfa"
done

limit=1000000
out=$(a_line 30 | "$engines" backtrack '(a+)+b' "$limit")
status=$?
printf 'n=30 limit=%s: status %s, %s\n' "$limit" "$status" "${out#*match=}"
