#!/usr/bin/env bats
# Tests for regex engine mechanics: step counts of the backtracking, memoized, Thompson and
# lazy-DFA matchers in engines.c, and what this system's grep, sed and awk do with the same
# pathological patterns. The utilities never depend on the shell under test, so the utility
# tests branch on the implementation (lab_awk_flavor), not on lab_shell_family.
# shellcheck disable=SC2016 # the single-quoted code is expanded by the shell under test

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  ENGINES="$BATS_TEST_TMPDIR/engines"
  BOUNDED="$BATS_TEST_TMPDIR/bounded"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -o "$ENGINES" "$LAB/engines.c"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -o "$BOUNDED" "$LAB/bounded.c"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

@test "race.sh: backtracking doubles per byte on (a+)+b, memo and Thompson grow by 4, a step limit stops it" {
  run_lab "$LAB/race.sh" "$ENGINES"
  [ "$status" -eq 0 ]
  [ "$output" = "  n  backtrack  memo thompson
  4         76    17       17
  8       1276    33       33
 12      20476    49       49
 16     327676    65       65
 20    5242876    81       81
n=30 limit=1000000: status 3, limit steps=1000001" ]
}

@test "closed forms: backtracking takes 5 * 2^n - 4 steps, the automata 4n + 1, for n = 1 to 18" {
  local n s want_bt want_lin
  for ((n = 1; n <= 18; n++)); do
    s=$(awk -v n="$n" 'BEGIN { s = ""; for (i = 0; i < n; i++) s = s "a"; print s }')
    want_bt=$((5 * (1 << n) - 4))
    want_lin=$((4 * n + 1))
    run_lab_c 'printf "%s\n" "$2" | "$1" backtrack "(a+)+b"' sh "$ENGINES" "$s"
    [ "$status" -eq 1 ]
    [ "$output" = "engine=backtrack nfa=5 match=0 end=-1 steps=$want_bt" ]
    run_lab_c 'printf "%s\n" "$2" | "$1" memo "(a+)+b"' sh "$ENGINES" "$s"
    [ "$status" -eq 1 ]
    [ "$output" = "engine=memo nfa=5 match=0 end=-1 steps=$want_lin" ]
    run_lab_c 'printf "%s\n" "$2" | "$1" thompson "(a+)+b"' sh "$ENGINES" "$s"
    [ "$status" -eq 1 ]
    [ "$output" = "engine=thompson nfa=5 match=0 end=-1 steps=$want_lin" ]
  done
}

@test "leftmost-first vs longest: on xyz, x|xy ends at 1 for the backtrackers and at 2 for the automata" {
  run_lab_c 'printf "xyz\n" | "$1" backtrack "x|xy"' sh "$ENGINES"
  [ "$status" -eq 0 ]
  [ "$output" = "engine=backtrack nfa=5 match=1 end=1 steps=3" ]
  run_lab_c 'printf "xyz\n" | "$1" memo "x|xy"' sh "$ENGINES"
  [ "$status" -eq 0 ]
  [ "$output" = "engine=memo nfa=5 match=1 end=1 steps=3" ]
  run_lab_c 'printf "xyz\n" | "$1" thompson "x|xy"' sh "$ENGINES"
  [ "$status" -eq 0 ]
  [ "$output" = "engine=thompson nfa=5 match=1 end=2 steps=6" ]
  run_lab_c 'printf "xyz\n" | "$1" dfa "x|xy" 8' sh "$ENGINES"
  [ "$status" -eq 0 ]
  [ "$output" = "engine=dfa nfa=5 match=1 end=2 steps=3 states=4 flushes=0" ]
}

@test "POSIX sed -E takes the longest alternative, as the automata do: x|xy on xyz replaces xy" {
  run_lab_c 'printf "xyz\n" | sed -E "s/x|xy/[&]/"'
  [ "$status" -eq 0 ]
  [ "$output" = "[xy]z" ]
}

@test "engines.c rejects a loop whose body can match the empty string" {
  run_lab_c 'printf "a\n" | "$1" backtrack "(a*)*"' sh "$ENGINES"
  [ "$status" -eq 2 ]
  [ "$output" = "engines: loop body can match the empty string" ]
}

@test "cache.sh: the k=10 DFA needs exactly 1024 states; a 1023-state cache thrashes, 256 thrashes more" {
  run_lab "$LAB/cache.sh" "$ENGINES" 10 256 1023 1024
  [ "$status" -eq 0 ]
  [ "$output" = "k=10 pattern (a|b)*a(a|b)(a|b)(a|b)(a|b)(a|b)(a|b)(a|b)(a|b)(a|b)
cap=256: nfa=33 states=87332 flushes=341
cap=1023: nfa=33 states=14551 flushes=14
cap=1024: nfa=33 states=1024 flushes=0" ]
}

@test "cache.sh: each extra (a|b) doubles the DFA (k=4: 16 states, k=8: 256, k=12: 4096)" {
  run_lab "$LAB/cache.sh" "$ENGINES" 4 100000
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "cap=100000: nfa=15 states=16 flushes=0" ]
  run_lab "$LAB/cache.sh" "$ENGINES" 8 100000
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "cap=100000: nfa=27 states=256 flushes=0" ]
  run_lab "$LAB/cache.sh" "$ENGINES" 12 100000
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "cap=100000: nfa=39 states=4096 flushes=0" ]
}

@test "bounded.c kills a command that outlives its bound and exits 124" {
  run_lab_c '"$1" 1 /bin/sleep 5; echo "status $?"' sh "$BOUNDED"
  [ "$status" -eq 0 ]
  [ "$output" = "status 124" ]
  run_lab_c '"$1" 5 /bin/sh -c "exit 7"; echo "status $?"' sh "$BOUNDED"
  [ "$output" = "status 7" ]
}

@test "tools.sh: grep -E and sed -E finish both nested patterns on a 5001-byte line (no backtracking)" {
  run_lab "$LAB/tools.sh" "$BOUNDED" 5000
  [ "$status" -eq 0 ]
  [ "$output" = "grep -E (a|aa)*c: status 1, 0 lines
sed -E (a|aa)*c: status 0, 0 lines
grep -E ^(a+)+$: status 1, 0 lines
sed -E ^(a+)+$: status 0, 0 lines" ]
}

@test "awk: every DFA or parallel-NFA awk finishes (a|aa)*c on 5001 bytes; mawk's backtracker does not" {
  local line="$BATS_TEST_TMPDIR/line" cmd flavor
  local -a awks=() words
  awk 'BEGIN { s = ""; for (i = 0; i < 5000; i++) s = s "a"; print s "!" }' >"$line"
  while IFS= read -r cmd; do
    awks+=("$cmd")
  done < <(lab_awks)
  [ "${#awks[@]}" -gt 0 ]
  for cmd in "${awks[@]}"; do
    IFS=' ' read -r -a words <<<"$cmd"
    flavor=$(lab_awk_flavor "${words[@]}")
    run_lab_c '"$@"; echo "status $?"' sh "$BOUNDED" 5 "${words[@]}" '/(a|aa)*c/ { print "match" }' "$line"
    case $flavor in
      mawk) [ "$output" = "status 124" ] ;;
      gawk | onetrue | busybox) [ "$output" = "status 0" ] ;;
      *) false ;;
    esac
  done
}
