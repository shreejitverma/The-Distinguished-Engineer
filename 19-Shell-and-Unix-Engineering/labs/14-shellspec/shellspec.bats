#!/usr/bin/env bats
# Tests for the ShellSpec model in this directory: translate.sh (spec -> plain script), minispec.sh
# (the example runtime) and defects.sh (shell-defect probes). Every test runs code through run_lab or
# run_lab_c, so the matrix can swap the shell (LAB_SHELL).

setup() {
  load ../lib/lab
  LAB=$BATS_TEST_DIRNAME
  export MS_LIB=$LAB
  export MS_WORK=$BATS_TEST_TMPDIR/work
  mkdir -p "$MS_WORK"
  SPEC=$BATS_TEST_TMPDIR/t_spec.sh
  SCRIPT=$BATS_TEST_TMPDIR/t.sh
}

# translate_file SPEC: translate a spec file into $SCRIPT and require success.
translate_file() {
  run_lab "$LAB/translate.sh" "$1" "$SCRIPT"
  [ "$status" -eq 0 ]
}

# translate_run: read a spec from stdin, translate it, then run the translated script.
translate_run() {
  cat >"$SPEC"
  translate_file "$SPEC"
  run_lab "$SCRIPT"
}

@test "translate emits a plain script: one function and one ms_example call per It" {
  translate_file "$LAB/spec/sample_spec.sh"
  functions=$(grep -c '^ms_ex[0-9]*() {$' "$SCRIPT")
  calls=$(grep -c '^ms_example ' "$SCRIPT")
  [ "$functions" -eq 3 ]
  [ "$calls" -eq 3 ]
  grep -qF "ms_example 'sample greet says hello' ms_ex1" "$SCRIPT"
  first=$(head -n 1 "$SCRIPT")
  last=$(tail -n 1 "$SCRIPT")
  # shellcheck disable=SC2016 # the dollar sign is literal text in the generated script
  [ "$first" = '. "$MS_LIB/minispec.sh"' ]
  [ "$last" = ms_end ]
}

@test "the sample spec prints one verdict per example and exits 101 when a warning exists" {
  translate_file "$LAB/spec/sample_spec.sh"
  run_lab "$SCRIPT"
  [ "$status" -eq 101 ]
  [ "${#lines[@]}" -eq 5 ]
  [ "${lines[0]}" = "ok 1 - sample greet says hello" ]
  [ "${lines[1]}" = "ok 2 - sample keeps a quote (') in the description" ]
  [ "${lines[2]}" = "warn 3 - sample reports an unhandled stdout as a warning" ]
  [ "${lines[3]}" = "  WARNING: unhandled stdout" ]
  [ "${lines[4]}" = "3 examples, 0 failures, 1 warnings" ]
}

@test "each example runs in its own subshell, so a variable never leaks into the next one" {
  translate_run <<'EOF'
Include subject.sh
It 'sets a variable'
  When call bump
  The variable COUNTER should eq 1
End
It 'starts from a fresh shell'
  The variable COUNTER should be undefined
End
EOF
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ok 1 - sets a variable" ]
  [ "${lines[1]}" = "ok 2 - starts from a fresh shell" ]
  [ "${lines[2]}" = "2 examples, 0 failures, 0 warnings" ]
}

@test "call keeps the function's variables, run discards them" {
  translate_run <<'EOF'
Include subject.sh
It 'call keeps the variable'
  When call bump
  The variable COUNTER should eq 1
End
It 'run discards the variable'
  When run bump
  The variable COUNTER should be undefined
End
EOF
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ok 1 - call keeps the variable" ]
  [ "${lines[1]}" = "ok 2 - run discards the variable" ]
}

@test "run catches exit and reports its status, call lets exit end the example" {
  translate_run <<'EOF'
Include subject.sh
Describe 'exit handling'
  It 'run catches exit'
    When run die3
    The status should eq 3
  End
  It 'call lets exit end the example'
    When call die3
    The status should eq 3
  End
End
EOF
  [ "$status" -eq 101 ]
  [ "${lines[0]}" = "ok 1 - exit handling run catches exit" ]
  [ "${lines[1]}" = "not ok 2 - exit handling call lets exit end the example (aborted: exit status 3)" ]
  [ "${lines[2]}" = "2 examples, 1 failures, 0 warnings" ]
}

@test "run enforces errexit, call ignores it" {
  translate_run <<'EOF'
Include subject.sh
It 'run stops at the first failing command'
  When run stops_early
  The stdout should eq ''
  The status should eq 1
End
It 'call ignores errexit and reaches the next command'
  When call stops_early
  The stdout should eq 'reached'
  The status should eq 0
End
EOF
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ok 1 - run stops at the first failing command" ]
  [ "${lines[1]}" = "ok 2 - call ignores errexit and reaches the next command" ]
}

@test "errexit is ignored inside a subshell that is not the last element of an AND-OR list" {
  run_lab_c '(set -e; false; echo reached) && :'
  [ "$status" -eq 0 ]
  [ "$output" = reached ]
}

@test "after set +e a subshell that sets -e itself does stop at the first failure" {
  run_lab_c 'set +e; (set -e; false; echo reached); echo "rc=$?"'
  [ "$status" -eq 0 ]
  [ "$output" = "rc=1" ]
}

@test "unhandled status, stdout and stderr and a missing expectation are warnings" {
  translate_run <<'EOF'
Include subject.sh
It 'ignores everything'
  When call noisy
End
It 'handles everything'
  When call noisy
  The status should be failure
  The stdout should eq 'out'
  The stderr should eq 'err'
End
It 'has no expectation'
  When call :
End
EOF
  [ "$status" -eq 101 ]
  [ "${#lines[@]}" -eq 9 ]
  [ "${lines[0]}" = "warn 1 - ignores everything" ]
  [ "${lines[1]}" = "  WARNING: no expectation found" ]
  [ "${lines[2]}" = "  WARNING: unhandled status 2" ]
  [ "${lines[3]}" = "  WARNING: unhandled stdout" ]
  [ "${lines[4]}" = "  WARNING: unhandled stderr" ]
  [ "${lines[5]}" = "ok 2 - handles everything" ]
  [ "${lines[6]}" = "warn 3 - has no expectation" ]
  [ "${lines[7]}" = "  WARNING: no expectation found" ]
  [ "${lines[8]}" = "3 examples, 0 failures, 2 warnings" ]
}

@test "stdout drops every trailing newline, entire stdout keeps them and a missing final one" {
  translate_run <<'EOF'
Include subject.sh
nonl() { printf 'x'; }
It 'stdout drops every trailing newline'
  When call trailing
  The stdout should eq 'a'
End
It 'entire stdout keeps all three'
  When call trailing
  The entire stdout should eq "a$MS_LF$MS_LF$MS_LF"
End
It 'entire stdout shows a missing final newline'
  When call nonl
  The entire stdout should eq 'x'
End
EOF
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ok 1 - stdout drops every trailing newline" ]
  [ "${lines[1]}" = "ok 2 - entire stdout keeps all three" ]
  [ "${lines[2]}" = "ok 3 - entire stdout shows a missing final newline" ]
}

@test "a command substitution strips every trailing newline (POSIX XCU 2.6.3)" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'x=$(printf "a\n\n\n"); printf "%s|" "$x"'
  [ "$status" -eq 0 ]
  [ "$output" = "a|" ]
}

@test "Data feeds stdin; without Data the example reads /dev/null and cannot hang" {
  translate_run <<'EOF'
Include subject.sh
It 'feeds Data to stdin'
  Data 'hi'
  When call slurp
  The stdout should eq 'hi'
End
It 'reads end of file without Data'
  When call slurp
  The stdout should eq ''
End
EOF
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ok 1 - feeds Data to stdin" ]
  [ "${lines[1]}" = "ok 2 - reads end of file without Data" ]
}

@test "status and stderr expectations, and a failing eq shows expected and actual" {
  translate_run <<'EOF'
Include subject.sh
It 'matches success'
  When call greet a
  The status should be success
  The stdout should eq 'hello a'
End
It 'matches failure and stderr'
  When call noisy
  The status should be failure
  The stdout should eq 'out'
  The stderr should eq 'err'
End
It 'fails with a message'
  When call greet a
  The stdout should eq 'nope'
End
It 'rejects an unknown matcher'
  When call greet a
  The stdout should eq 'hello a'
  The status should be bogus
End
EOF
  [ "$status" -eq 101 ]
  [ "${lines[0]}" = "ok 1 - matches success" ]
  [ "${lines[1]}" = "ok 2 - matches failure and stderr" ]
  [ "${lines[2]}" = "not ok 3 - fails with a message" ]
  [ "${lines[3]}" = "  expected 'nope' but got 'hello a'" ]
  [ "${lines[4]}" = "not ok 4 - rejects an unknown matcher" ]
  [ "${lines[5]}" = "  expectation not met: unknown matcher be bogus (subject '0')" ]
  [ "${lines[6]}" = "4 examples, 2 failures, 0 warnings" ]
}

@test "a second When, an unknown evaluation type and an unknown subject are syntax errors" {
  translate_run <<'EOF'
Include subject.sh
It 'has two evaluations'
  When call greet a
  When call greet b
  The stdout should eq 'hello a'
End
It 'has an unknown evaluation type'
  When bogus greet a
End
It 'has an unknown subject'
  When call greet a
  The bogus should eq 1
End
EOF
  [ "$status" -eq 101 ]
  [ "${lines[0]}" = "not ok 1 - has two evaluations" ]
  [ "${lines[1]}" = "  syntax error: only one When per example" ]
  [ "${lines[2]}" = "not ok 2 - has an unknown evaluation type" ]
  [ "${lines[3]}" = "  syntax error: unknown evaluation type bogus" ]
  [ "${lines[4]}" = "not ok 3 - has an unknown subject" ]
  [ "${lines[5]}" = "  syntax error: unknown subject bogus" ]
  [ "${lines[6]}" = "3 examples, 3 failures, 0 warnings" ]
}

@test "translate rejects a stray End, an unterminated block and a wrong argument count" {
  printf 'End\n' >"$SPEC"
  run_lab "$LAB/translate.sh" "$SPEC" "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "Syntax error: unexpected End at line 1" ]

  printf "Describe 'x'\n" >"$SPEC"
  run_lab "$LAB/translate.sh" "$SPEC" "$SCRIPT"
  [ "$status" -eq 1 ]
  [ "$output" = "Syntax error: unexpected end of file (expecting 'End')" ]

  run_lab "$LAB/translate.sh" "$SPEC"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: translate.sh SPEC OUT" ]
}

@test "the defect probes fire per shell, each computed from the shell's own behavior" {
  run_lab "$LAB/defects.sh"
  [ "$status" -eq 0 ]
  [ "${lines[$((${#lines[@]} - 1))]}" = "done" ]
  local line
  for line in "${lines[@]}"; do
    case $line in
      EMPTYPARAMS | REDEFINE | BUILTIN | SHELLFLAG | ERREXIT | SUBSHELL | SETE | DEBUG_TRAP | done) ;;
      *) false ;;
    esac
  done
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        3.2* | 5.*) [ "$output" = "$(printf 'DEBUG_TRAP\ndone')" ] ;;
        4.*) printf '%s\n' "${lines[@]}" | grep -qx DEBUG_TRAP ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh) [ "$output" = "$(printf 'DEBUG_TRAP\ndone')" ] ;;
    dash) [ "$output" = "done" ] ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) ;; # not measured here: only the invariants above apply
        *93u+*) [ "$output" = "$(printf 'REDEFINE\nDEBUG_TRAP\ndone')" ] ;;
        *) false ;;
      esac
      ;;
    posh) printf '%s\n' "${lines[@]}" | grep -qx EMPTYPARAMS ;;
    mksh | yash | busybox) ;; # not measured here: only the invariants above apply
    *) false ;;
  esac
}
