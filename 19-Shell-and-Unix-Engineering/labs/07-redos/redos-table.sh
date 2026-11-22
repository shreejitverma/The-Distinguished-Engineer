#!/bin/sh
# redos-table.sh BIN: print the work each strategy does as the subject grows.
# BIN is the compiled redos-steps program.
#   Regex (a|a)*b against a^n c: backtracking (bt) versus Thompson simulation (nfa).
#   Shell pattern *a*a*a*b against a^n: naive star retry versus linear last-star restart.
# shellcheck disable=SC2312 # every row prints computed step counts; a failed run shows up as a missing value
bin=$1

steps() {
  "$bin" "$@" | sed 's/^steps=\([0-9]*\) .*/\1/'
}

printf '%-4s %12s %6s\n' n bt nfa
for n in 8 12 16 20; do
  printf '%-4s %12s %6s\n' "$n" "$(steps bt '(a|a)*b' a "$n" c)" "$(steps nfa '(a|a)*b' a "$n" c)"
done
printf '%-4s %12s %6s\n' n glob-naive linear
for n in 10 20 40 80; do
  printf '%-4s %12s %6s\n' "$n" "$(steps glob-naive '*a*a*a*b' a "$n" '')" "$(steps glob-linear '*a*a*a*b' a "$n" '')"
done
