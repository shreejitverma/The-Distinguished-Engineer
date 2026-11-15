#!/bin/sh
# cache.sh ENGINES K CAP...: run the lazy DFA of engines.c for (a|b)*a(a|b)...(a|b), with K-1
# copies of (a|b) after the a, over 100000 pseudo-random a/b bytes, once per cache size CAP.
# The pattern says "the byte K positions back was an a", so its DFA has 2^K states.
set -u
engines=${1:?usage: cache.sh ENGINES K CAP...}
k=${2:?usage: cache.sh ENGINES K CAP...}
shift 2

pattern='(a|b)*a'
i=1
while [ "$i" -lt "$k" ]; do
  pattern="$pattern(a|b)"
  i=$((i + 1))
done
printf 'k=%s pattern %s\n' "$k" "$pattern"

for cap in "$@"; do
  out=$("$engines" gen 100000 | "$engines" dfa "$pattern" "$cap") || :
  nfa=${out#*nfa=}
  states=${out#*states=}
  flushes=${out#*flushes=}
  printf 'cap=%s: nfa=%s states=%s flushes=%s\n' "$cap" "${nfa%% *}" "${states%% *}" "$flushes"
done
