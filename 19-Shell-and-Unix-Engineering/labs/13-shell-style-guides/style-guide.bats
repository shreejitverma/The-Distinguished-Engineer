#!/usr/bin/env bats
# Tests for Shell Style Guides: verifying key mechanisms enforced by corporate
# and community shell style guides across shell implementations.
# Code under test runs only through run_lab SCRIPT or run_lab_c CODE (labs/lib/lab.bash).

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/style-probe.sh"
}

@test "POSIX function definition fname() is portable across all shells, while the function keyword is an extension" {
  family=$(lab_shell_family)
  run_lab "$PROBE" func_syntax
  [ "$status" -eq 0 ]
  probe_output=$output
  case $family in
    bash | zsh | zsh-sh | mksh | yash)
      [ "$probe_output" = "posix=ok korn=ok hybrid=ok" ]
      ;;
    ksh93)
      # ksh93 accepts POSIX and Korn syntax, but rejects hybrid function fname() with syntax error
      [ "$probe_output" = "posix=ok korn=ok hybrid=error" ]
      ;;
    dash | posh)
      [ "$probe_output" = "posix=ok korn=error hybrid=error" ]
      ;;
    busybox)
      # The function keyword exists only when BusyBox is built with CONFIG_ASH_BASH_COMPAT (default y)
      run_lab_c 'function f { :; }; f'
      if [ "$status" -eq 0 ]; then
        [ "$probe_output" = "posix=ok korn=ok hybrid=ok" ]
      else
        [ "$probe_output" = "posix=ok korn=error hybrid=error" ]
      fi
      ;;
    *) false ;;
  esac
}

@test "declaration builtins local, export, and readonly mask command substitution exit status" {
  family=$(lab_shell_family)
  run_lab "$PROBE" mask_return
  [ "$status" -eq 0 ]
  case $family in
    bash | zsh | zsh-sh | yash | mksh | busybox | posh | dash)
      [ "$output" = "masked=0 separate=42 export=0 readonly=0" ]
      ;;
    ksh93)
      # ksh93 lacks local builtin, so local assignment fails with status 127
      [ "$output" = "masked=127 separate=42 export=0 readonly=0" ]
      ;;
    *) false ;;
  esac
}

@test "shell functions exhibit dynamic scoping where caller local variables are visible to callees" {
  family=$(lab_shell_family)
  run_lab "$PROBE" scope_leak
  [ "$status" -eq 0 ]
  case $family in
    bash | zsh | zsh-sh | yash | mksh | busybox | posh | dash)
      [ "$output" = "inner_read=initial_outer outer_after=mutated_by_inner global=global_val" ]
      ;;
    ksh93)
      # ksh93 lacks local builtin, so local assignment fails and scoped_var remains global
      [ "$output" = "inner_read=initial_outer outer_after=mutated_by_inner global=mutated_by_inner" ]
      ;;
    *) false ;;
  esac
}

@test "uppercase variable collisions affect system variables while lowercase variables remain isolated" {
  family=$(lab_shell_family)
  run_lab "$PROBE" case_collision
  [ "$status" -eq 0 ]
  case $family in
    bash | zsh | zsh-sh)
      # bash: UID is readonly; zsh (also in sh emulation): UID is special and assigning it calls
      # setuid(2), which fails for an unprivileged user. Both give status 1.
      [ "$output" = "uid_status=1 lowercase=app_1234 path_clobber_status=127" ]
      ;;
    yash | mksh | busybox | posh | ksh93 | dash)
      # UID is an ordinary variable in these shells
      [ "$output" = "uid_status=0 lowercase=app_1234 path_clobber_status=127" ]
      ;;
    *) false ;;
  esac
}

@test "an unquoted empty operand vanishes from the test builtin so -n tests itself, while the [[ keyword keeps it" {
  family=$(lab_shell_family)
  run_lab "$PROBE" bracket_test
  [ "$status" -eq 0 ]
  probe_output=$output
  case $family in
    bash | zsh | zsh-sh | ksh93 | mksh | yash)
      [ "$probe_output" = "single_unquoted=0 single_quoted=1 double_unquoted=1" ]
      ;;
    dash | posh | busybox)
      # No [[ keyword. dash and posh (pdksh code compiled out) look the word [[ up as a command:
      # not found (127), except on Alpine, where BusyBox installs /usr/bin/[[ as a test applet link.
      # BusyBox ash has [[ only as a regular builtin (the test applet). Wherever a [[ command runs,
      # the unquoted empty operand still vanishes and the lone operand -n is true, as with [
      run_lab_c '[[ -n x ]]'
      if [ "$status" -eq 0 ]; then
        [ "$probe_output" = "single_unquoted=0 single_quoted=1 double_unquoted=0" ]
      else
        [ "$probe_output" = "single_unquoted=0 single_quoted=1 double_unquoted=127" ]
      fi
      ;;
    *) false ;;
  esac
}

@test "main encapsulation prevents premature execution when a script is truncated before the entry point" {
  family=$(lab_shell_family)
  # 1. Truncated guarded script (functions defined, but main "$@" not yet reached)
  run_lab "$PROBE" main_guard guarded "$BATS_TEST_TMPDIR/guarded.sh"
  [ "$status" -eq 0 ]
  run_lab "$BATS_TEST_TMPDIR/guarded.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
  # Control: the same file with the entry-point call restored runs both steps, so the empty
  # output above is truncation protection and not a broken script
  printf 'main "$@"\n' >>"$BATS_TEST_TMPDIR/guarded.sh"
  run_lab "$BATS_TEST_TMPDIR/guarded.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'step1\nstep2')" ]

  # 2. Truncated linear script (executes top-level lines immediately)
  run_lab "$PROBE" main_guard linear "$BATS_TEST_TMPDIR/linear.sh"
  [ "$status" -eq 0 ]
  run_lab "$BATS_TEST_TMPDIR/linear.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "step1" ]

  case $family in
    bash | zsh | zsh-sh | yash | mksh | busybox | posh | ksh93 | dash)
      ;;
    *) false ;;
  esac
}

@test "array and positional parameter quoting preserves whitespace and prevents unintended pathname globbing" {
  family=$(lab_shell_family)
  run_lab "$PROBE" array_quote
  [ "$status" -eq 0 ]
  case $family in
    zsh)
      # Native zsh does not field split unquoted parameter expansions
      [ "$output" = "quoted=2 unquoted=2" ]
      ;;
    bash | zsh-sh | yash | mksh | busybox | posh | ksh93 | dash)
      [ "$output" = "quoted=2 unquoted=5" ]
      ;;
    *) false ;;
  esac
}

@test "a loop at the end of a pipeline loses its assignments except in native zsh and ksh93, which run the last element in the current shell" {
  family=$(lab_shell_family)
  run_lab "$PROBE" pipe_while
  [ "$status" -eq 0 ]
  case $family in
    bash | zsh-sh | yash | mksh | busybox | posh | dash)
      [ "$output" = "last_line=NULL" ]
      ;;
    zsh | ksh93)
      [ "$output" = "last_line=b" ]
      ;;
    *) false ;;
  esac
}

@test "a standalone (( i++ )) with i=0 returns status 1 where the syntax exists and parses as nested subshells elsewhere" {
  family=$(lab_shell_family)
  run_lab "$PROBE" arith_zero
  [ "$status" -eq 0 ]
  case $family in
    bash | zsh | zsh-sh | mksh | ksh93)
      [ "$output" = "postinc_status=1" ]
      ;;
    dash | posh | yash | busybox)
      # No (( )) command: the shell reads two nested subshells and fails to find a command i++
      [ "$output" = "postinc_status=127" ]
      ;;
    *) false ;;
  esac
}
