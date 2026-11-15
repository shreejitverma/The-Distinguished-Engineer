#!/usr/bin/env bats
# ReDoS lab: count backtracking versus automaton work exactly (redos-steps.c), then show
# that a multi-star 'case' pattern exhausts a CPU budget in shells whose matcher
# backtracks across stars, and that committing one star at a time does not.

setup() {
  load ../lib/lab
  BIN="$BATS_TEST_TMPDIR/redos-steps"
  "${CC:-cc}" -std=c99 -O2 -Wall -Wextra -Werror -o "$BIN" "$BATS_TEST_DIRNAME/redos-steps.c"
  GLOB_CASE="$BATS_TEST_DIRNAME/glob-case.sh"
}

@test "redos-table.sh prints exponential bt, linear nfa, polynomial naive glob, linear glob" {
  run_lab "$BATS_TEST_DIRNAME/redos-table.sh" "$BIN"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "n              bt    nfa" ]
  [ "${lines[1]}" = "8            4086    105" ]
  [ "${lines[2]}" = "12          65526    153" ]
  [ "${lines[3]}" = "16        1048566    201" ]
  [ "${lines[4]}" = "20       16777206    249" ]
  [ "${lines[5]}" = "n      glob-naive linear" ]
  [ "${lines[6]}" = "10            737     22" ]
  [ "${lines[7]}" = "20           8897     42" ]
  [ "${lines[8]}" = "40         123492     82" ]
  [ "${lines[9]}" = "80        1837782    162" ]
  [ "${#lines[@]}" -eq 10 ]
}

@test "one more byte doubles backtracking work on (a|a)*b but adds 12 NFA steps" {
  run_lab_c '"$@"' sh "$BIN" bt '(a|a)*b' a 13 c
  [ "$status" -eq 1 ]
  [ "$output" = "steps=131062 result=nomatch" ]
  run_lab_c '"$@"' sh "$BIN" bt '(a|a)*b' a 14 c
  [ "$output" = "steps=262134 result=nomatch" ]
  run_lab_c '"$@"' sh "$BIN" nfa '(a|a)*b' a 13 c
  [ "$output" = "steps=165 result=nomatch" ]
  run_lab_c '"$@"' sh "$BIN" nfa '(a|a)*b' a 14 c
  [ "$output" = "steps=177 result=nomatch" ]
}

@test "nested quantifier (a+)+b is exponential only when the match fails" {
  run_lab_c '"$@"' sh "$BIN" bt '(a+)+b' a 16 c
  [ "$output" = "steps=524281 result=nomatch" ]
  run_lab_c '"$@"' sh "$BIN" bt '(a+)+b' a 17 c
  [ "$output" = "steps=1048569 result=nomatch" ]
  run_lab_c '"$@"' sh "$BIN" bt '(a+)+b' a 16 b
  [ "$status" -eq 0 ]
  [ "$output" = "steps=56 result=match" ]
  run_lab_c '"$@"' sh "$BIN" nfa '(a+)+b' a 16 b
  [ "$status" -eq 0 ]
  [ "$output" = "steps=148 result=match" ]
}

@test "a step budget turns a runaway backtrack into an error, like PCRE2's match limit" {
  run_lab_c '"$@"' sh "$BIN" bt '(a|a)*b' a 30 c 1000000
  [ "$status" -eq 3 ]
  [ "$output" = "steps=1000000 result=limit" ]
  run_lab_c '"$@"' sh "$BIN" nfa '(a|a)*b' a 30 c 1000000
  [ "$status" -eq 1 ]
  [ "$output" = "steps=369 result=nomatch" ]
}

@test "an empty-matching loop (a*)* recurses without progress until the depth limit" {
  run_lab_c '"$@"' sh "$BIN" bt '(a*)*b' a 3 c
  [ "$status" -eq 3 ]
  [ "$output" = "steps=39995 result=depth" ]
  run_lab_c '"$@"' sh "$BIN" nfa '(a*)*b' a 3 c
  [ "$status" -eq 1 ]
  [ "$output" = "steps=35 result=nomatch" ]
}

@test "naive and linear glob matchers agree on the answer and differ in work" {
  run_lab_c '"$@"' sh "$BIN" glob-naive '*a*a*a*b' a 5 b
  [ "$status" -eq 0 ]
  [ "$output" = "steps=11 result=match" ]
  run_lab_c '"$@"' sh "$BIN" glob-linear '*a*a*a*b' a 5 b
  [ "$status" -eq 0 ]
  [ "$output" = "steps=12 result=match" ]
  run_lab_c '"$@"' sh "$BIN" glob-naive '*a*a*a*b' a 2000 '' 1000000
  [ "$status" -eq 3 ]
  [ "$output" = "steps=1000000 result=limit" ]
  run_lab_c '"$@"' sh "$BIN" glob-linear '*a*a*a*b' a 2000 ''
  [ "$status" -eq 1 ]
  [ "$output" = "steps=4002 result=nomatch" ]
}

@test "redos-steps rejects a malformed pattern and an unknown mode" {
  run_lab_c '"$@"' sh "$BIN" bt 'a(' a 1 ''
  [ "$status" -eq 2 ]
  [ "$output" = "redos-steps: bad PATTERN" ]
  run_lab_c '"$@"' sh "$BIN" xx a a 1 ''
  [ "$status" -eq 2 ]
  [ "$output" = "redos-steps: unknown MODE xx" ]
}

@test "both glob-case.sh modes give the right answer on a small subject" {
  run_lab "$GLOB_CASE" multi 5 3 2 b
  [ "$status" -eq 0 ]
  [ "$output" = "match" ]
  run_lab "$GLOB_CASE" commit 5 3 2 b
  [ "$status" -eq 0 ]
  [ "$output" = "match" ]
  run_lab "$GLOB_CASE" multi 2 3 2 b
  [ "$status" -eq 0 ]
  [ "$output" = "nomatch" ]
  run_lab "$GLOB_CASE" commit 2 3 2 b
  [ "$status" -eq 0 ]
  [ "$output" = "nomatch" ]
  run_lab "$GLOB_CASE" bogus 1 1 2
  [ "$status" -eq 2 ]
}

@test "a 4-star case pattern on 2000 bytes exhausts 2 CPU seconds where the matcher backtracks" {
  local family version
  family=$(lab_shell_family)
  if [ "$family" = posh ]; then
    # posh 0.14.1 has no ulimit builtin, so bash sets the soft limit and execs it
    lab_shell_words
    run "$BASH" -c 'ulimit -S -t 2 && exec "$@"' bash "${LAB_SHELL_WORDS[@]}" "$GLOB_CASE" multi 2000 3 2
  else
    run_lab "$GLOB_CASE" multi 2000 3 2
  fi
  case $family in
    bash)
      version=$(lab_shell_version)
      case $version in
        [34].*)
          # sm_loop.c gained the glibc-style "ends" shortcut in bash 5.0
          [ "$status" -eq 152 ]
          [ "$output" = "" ]
          ;;
        *)
          [ "$status" -eq 0 ]
          [ "$output" = "nomatch" ]
          ;;
      esac
      ;;
    busybox | yash)
      # BusyBox ash calls libc fnmatch(); yash compiles the pattern with regcomp()
      [ "$status" -eq 0 ]
      [ "$output" = "nomatch" ]
      ;;
    dash | zsh | zsh-sh | ksh93 | mksh | posh)
      # killed by SIGXCPU (24): 128 + 24
      [ "$status" -eq 152 ]
      [ "$output" = "" ]
      ;;
    *) false ;;
  esac
}

@test "committing one star at a time finishes in every shell" {
  run_lab "$GLOB_CASE" commit 2000 3 2
  [ "$status" -eq 0 ]
  [ "$output" = "nomatch" ]
}
