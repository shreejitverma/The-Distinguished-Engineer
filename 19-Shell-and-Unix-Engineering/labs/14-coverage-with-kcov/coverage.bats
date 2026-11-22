#!/usr/bin/env bats
# Tests for the kcov-style bash line tracer: the PS4 and DEBUG collection methods and the line report.
# kcov itself is not required; these tests exercise the mechanism kcov's bash engine is built on.

setup() {
  load ../lib/lab
  LAB=$BATS_TEST_DIRNAME
  TRACE=$BATS_TEST_TMPDIR/trace
}

# line_of TEXT: print the line number of the first line of target.sh that contains TEXT.
line_of() {
  awk -v p="$1" 'index($0, p) { print NR; exit }' "$LAB/target.sh"
}

# traced_bash_has_xtracefd: succeed when the traced bash is 4.1 or newer (BASH_XTRACEFD exists).
# bash NEWS lists BASH_XTRACEFD under bash-4.1; kcov's own version check is the stricter 4.2.
traced_bash_has_xtracefd() {
  local v major minor
  # shellcheck disable=SC2016 # the expansions belong to the traced bash
  v=$("${TRACE_BASH:-bash}" -c 'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"') || return 1
  major=${v%%.*}
  minor=${v#*.}
  [ "$major" -gt 4 ] || { [ "$major" -eq 4 ] && [ "$minor" -ge 1 ]; }
}

@test "the ps4 method writes kcov@FILE@LINE@ for each executed line and none for skipped lines" {
  traced_bash_has_xtracefd
  run_lab "$LAB/collect.sh" ps4 "$TRACE" "$LAB/target.sh" 7
  [ "$status" -eq 0 ]
  hit=$(line_of 'echo positive')
  skipped=$(line_of 'echo zero')
  grep -F -q -- "kcov@$LAB/target.sh@$hit@" "$TRACE"
  if grep -F -q -- "kcov@$LAB/target.sh@$skipped@" "$TRACE"; then false; fi
}

@test "with BASH_XTRACEFD the trace stays out of the script's stderr" {
  traced_bash_has_xtracefd
  run_lab "$LAB/collect.sh" ps4 "$TRACE" "$LAB/target.sh" 7
  [ "$status" -eq 0 ]
  case $output in
    *kcov@*) false ;;
    *positive*) ;;
    *) false ;;
  esac
  [ -s "$TRACE" ]
}

@test "a bash older than 4.1 ignores BASH_XTRACEFD and sends the trace to stderr" {
  # shellcheck disable=SC2016 # the expansion belongs to the traced bash
  v=$("${TRACE_BASH:-bash}" -c 'printf "%s" "$BASH_VERSION"')
  case $v in
    3.* | 4.0*)
      run_lab "$LAB/collect.sh" ps4 "$TRACE" "$LAB/target.sh" 7
      [ ! -s "$TRACE" ]
      case $output in *kcov@*) ;; *) false ;; esac
      ;;
    *)
      run_lab "$LAB/collect.sh" ps4 "$TRACE" "$LAB/target.sh" 7
      [ -s "$TRACE" ]
      case $output in *kcov@*) false ;; *) ;; esac
      ;;
  esac
}

@test "BASH_XTRACEFD redirects the trace in bash 4.1 or newer and BusyBox ash; other shells trace to stderr" {
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'exec 7>"$1"; BASH_XTRACEFD=7; PS4="kcov@x@"; set -x; : marker' name "$TRACE"
  [ "$status" -eq 0 ]
  family=$(lab_shell_family)
  case $family in
    bash)
      v=$(lab_shell_version)
      major=${v%%.*}
      rest=${v#*.}
      minor=${rest%%.*}
      if [ "$major" -gt 4 ] || { [ "$major" -eq 4 ] && [ "$minor" -ge 1 ]; }; then
        traced=$(cat "$TRACE")
        [ "$traced" = "kcov@x@: marker" ]
        [ "$output" = "" ]
      else
        [ ! -s "$TRACE" ]
        [ "$output" = "kcov@x@: marker" ]
      fi
      ;;
    busybox)
      # BusyBox ash honors BASH_XTRACEFD too
      traced=$(cat "$TRACE")
      [ "$traced" = "kcov@x@: marker" ]
      [ "$output" = "" ]
      ;;
    yash)
      # yash quotes the traced words
      [ ! -s "$TRACE" ]
      [ "$output" = "kcov@x@':' marker" ]
      ;;
    zsh | zsh-sh | posh | mksh | ksh93 | dash)
      [ ! -s "$TRACE" ]
      [ "$output" = "kcov@x@: marker" ]
      ;;
    *) false ;;
  esac
}

@test "PS4 is parameter-expanded for each trace line, except in native zsh and posh" {
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'PS4="kcov@\${LINENO}@"; set -x; : marker'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh)
      # native zsh runs PS4 through prompt expansion without PROMPT_SUBST, so ${LINENO} stays literal
      # shellcheck disable=SC2016 # the literal text is the expected output
      [ "$output" = 'kcov@${LINENO}@: marker' ]
      ;;
    posh)
      # posh 0.14.1 writes PS4 without any expansion
      # shellcheck disable=SC2016 # the literal text is the expected output
      [ "$output" = 'kcov@${LINENO}@: marker' ]
      ;;
    dash)
      # Debian builds dash without LINENO support, so it expands to nothing there
      case $output in
        "kcov@@: marker" | kcov@[0-9]*@": marker") ;;
        *) false ;;
      esac
      ;;
    yash)
      case $output in
        kcov@[0-9]*@"':' marker") ;;
        *) false ;;
      esac
      ;;
    bash | zsh-sh | busybox | mksh | ksh93)
      case $output in
        kcov@[0-9]*@*": marker") ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "nested expansion levels repeat the first PS4 character, so markers can start with kkcov@" {
  traced_bash_has_xtracefd
  run_lab "$LAB/collect.sh" ps4 "$TRACE" "$LAB/target.sh" 7
  [ "$status" -eq 0 ]
  nested=$(grep -c '^kkcov@' "$TRACE")
  anchored=$(grep -c '^kcov@' "$TRACE")
  total=$(grep -c 'kcov@' "$TRACE")
  [ "$nested" -ge 1 ]
  [ "$total" -gt "$anchored" ]
}

@test "the ps4 method also traces child shells through BASH_ENV, with an empty file field for bash -c" {
  traced_bash_has_xtracefd
  run_lab "$LAB/collect.sh" ps4 "$TRACE" "$LAB/target.sh" 7
  [ "$status" -eq 0 ]
  grep -q '^kcov@@1@' "$TRACE"
}

@test "the debug method unsets BASH_ENV so child shells are not traced" {
  run_lab "$LAB/collect.sh" debug "$TRACE" "$LAB/target.sh" 7
  [ "$status" -eq 0 ]
  [ -s "$TRACE" ]
  if grep -q '^kcov@@' "$TRACE"; then false; fi
}

@test "the debug method needs functrace to see lines inside functions" {
  script=$BATS_TEST_TMPDIR/fn.sh
  printf '%s\n' 'f() {' '  : inside' '}' 'f' >"$script"
  grep -v functrace "$LAB/hook-debug.sh" >"$BATS_TEST_TMPDIR/hook-nofunctrace.sh"

  BASH_ENV=$LAB/hook-debug.sh KCOV_BASH_XTRACEFD=7 run "${TRACE_BASH:-bash}" "$script" 7>"$TRACE.with"
  [ "$status" -eq 0 ]
  grep -F -q -- "kcov@$script@2@" "$TRACE.with"

  BASH_ENV=$BATS_TEST_TMPDIR/hook-nofunctrace.sh KCOV_BASH_XTRACEFD=7 run "${TRACE_BASH:-bash}" "$script" 7>"$TRACE.without"
  [ "$status" -eq 0 ]
  grep -F -q -- "kcov@$script@4@" "$TRACE.without"
  if grep -F -q -- "kcov@$script@2@" "$TRACE.without"; then false; fi
}

@test "the report marks executed, missed, and non-code lines" {
  traced_bash_has_xtracefd
  run_lab "$LAB/collect.sh" ps4 "$TRACE" "$LAB/target.sh" 7
  [ "$status" -eq 0 ]
  run_lab "$LAB/report.sh" "$LAB/target.sh" "$TRACE"
  [ "$status" -eq 0 ]
  printf '%s\n' "$output" | grep -q '^+ .* echo positive ;;$'
  printf '%s\n' "$output" | grep -q '^- .* echo zero ;;$'
  printf '%s\n' "$output" | grep -q '^- .* echo "failing"$'
  printf '%s\n' "$output" | grep -q '^\. .* heredoc line one$'
  printf '%s\n' "$output" | grep -q '^\. .* esac$'
  printf '%s\n' "$output" | grep -q '^\. .* classify() {$'
  [ "${output##*$'\n'}" = "12/17 lines covered (70%)" ]
}

@test "the debug and ps4 methods report the same coverage for the target" {
  traced_bash_has_xtracefd
  run_lab "$LAB/collect.sh" ps4 "$TRACE.ps4" "$LAB/target.sh" 7
  [ "$status" -eq 0 ]
  run_lab "$LAB/collect.sh" debug "$TRACE.dbg" "$LAB/target.sh" 7
  [ "$status" -eq 0 ]
  run_lab "$LAB/report.sh" "$LAB/target.sh" "$TRACE.ps4"
  ps4_report=$output
  run_lab "$LAB/report.sh" "$LAB/target.sh" "$TRACE.dbg"
  [ "$output" = "$ps4_report" ]
  [ "${output##*$'\n'}" = "12/17 lines covered (70%)" ]
}

@test "the union of two traces is the coverage of the merged runs" {
  traced_bash_has_xtracefd
  run_lab "$LAB/collect.sh" ps4 "$TRACE.a" "$LAB/target.sh" 7
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2030,SC2031 # bats runs each test in its own subshell
  export FAIL=1
  run_lab "$LAB/collect.sh" ps4 "$TRACE.b" "$LAB/target.sh"
  [ "$status" -eq 3 ]
  run_lab "$LAB/report.sh" "$LAB/target.sh" "$TRACE.b"
  [ "${output##*$'\n'}" = "11/17 lines covered (64%)" ]
  run_lab "$LAB/report.sh" "$LAB/target.sh" "$TRACE.a" "$TRACE.b"
  [ "${output##*$'\n'}" = "15/17 lines covered (88%)" ]
  printf '%s\n' "$output" | grep -q '^- .* echo zero ;;$'
}

@test "the collector passes the traced script's exit status through" {
  traced_bash_has_xtracefd
  # shellcheck disable=SC2030,SC2031 # bats runs each test in its own subshell
  export FAIL=1
  run_lab "$LAB/collect.sh" ps4 "$TRACE" "$LAB/target.sh" 7
  [ "$status" -eq 3 ]
}

@test "an unknown collection method is rejected" {
  run_lab "$LAB/collect.sh" bogus "$TRACE" "$LAB/target.sh"
  [ "$status" -eq 2 ]
  [ "$output" = "collect: unknown method bogus" ]
}

@test "a file name containing @ breaks splitting a marker on @" {
  traced_bash_has_xtracefd
  script=$BATS_TEST_TMPDIR/a@b.sh
  printf '%s\n' '#!/bin/sh' 'echo hi' >"$script"
  run_lab "$LAB/collect.sh" ps4 "$TRACE" "$script"
  [ "$status" -eq 0 ]
  grep -F -q -- "kcov@$script@2@" "$TRACE"
  # kcov splits "kcov@FILE@LINE@..." on "@" and reads field 2 as the line number
  field=$(awk -F@ 'NR == 1 { print $3 }' "$TRACE")
  [ "$field" = "b.sh" ]
  run_lab "$LAB/report.sh" "$script" "$TRACE"
  printf '%s\n' "$output" | grep -q '^+   2 echo hi$'
}
