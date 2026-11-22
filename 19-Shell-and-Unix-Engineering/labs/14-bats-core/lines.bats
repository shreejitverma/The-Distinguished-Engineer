#!/usr/bin/env bats
# Tests for lines.sh and interp.sh: bats runs each test in bash, and `run_lab` runs the code under
# test in the shell the matrix selects.

setup() {
  load ../lib/lab
  bats_require_minimum_version 1.5.0
  LINES="$BATS_TEST_DIRNAME/lines.sh"
  INTERP="$BATS_TEST_DIRNAME/interp.sh"
}

@test "no operand is a usage error with status 64 and the usage on stderr" {
  run_lab "$LINES"
  [ "$status" -eq 64 ]
  [ "$output" = "usage: lines [-n] FILE|-" ]
}

@test "a file name with spaces is counted and echoed intact" {
  printf 'a\nb\nc\n' >"$BATS_TEST_TMPDIR/my file.txt"
  run_lab "$LINES" "$BATS_TEST_TMPDIR/my file.txt"
  [ "$status" -eq 0 ]
  [ "$output" = "3 $BATS_TEST_TMPDIR/my file.txt" ]
}

@test "-n prints the bare number" {
  printf 'a\nb\n' >"$BATS_TEST_TMPDIR/f"
  run_lab "$LINES" -n "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  [ "$output" = 2 ]
}

@test "the operand - reads standard input" {
  printf 'x\ny\nz\nw\n' >"$BATS_TEST_TMPDIR/in"
  run_lab "$LINES" -n - <"$BATS_TEST_TMPDIR/in"
  [ "$status" -eq 0 ]
  [ "$output" = 4 ]
}

@test "run -2 --separate-stderr keeps the diagnostic out of output" {
  missing="$BATS_TEST_TMPDIR/missing"
  # run's flags must precede the command, so this test splits LAB_SHELL itself instead of run_lab.
  lab_shell_words
  run -2 --separate-stderr "${LAB_SHELL_WORDS[@]}" "$LINES" "$missing"
  [ -z "$output" ]
  # shellcheck disable=SC2154 # stderr is set by run --separate-stderr
  [ "$stderr" = "lines: cannot read: $missing" ]
}

@test "run without --separate-stderr merges the diagnostic into output" {
  missing="$BATS_TEST_TMPDIR/missing"
  run_lab "$LINES" "$missing"
  [ "$status" -eq 2 ]
  [ "$output" = "lines: cannot read: $missing" ]
}

@test "the script runs under the shell under test while the test itself runs in bash" {
  [ -n "${BASH_VERSION-}" ]
  run_lab "$INTERP"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash) [ "$output" = bash ] ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "the bats executable itself refuses to run under a shell that is not bash" {
  # inside a test PATH starts with bats' libexec directory, whose `bats` is the internal script
  clean_path=":$PATH:"
  clean_path=${clean_path//":$BATS_LIBEXEC:"/:}
  clean_path=${clean_path#:}
  clean_path=${clean_path%:}
  bats_path=$(PATH=$clean_path command -v bats)
  # bats starts with `set -euo pipefail`; a shell without pipefail dies there, before its own check.
  pipefail=no
  run_lab_c 'set -o pipefail'
  if [ "$status" -eq 0 ]; then
    pipefail=yes
  fi
  run_lab "$bats_path" --version
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      # shellcheck disable=SC2153 # BATS_VERSION is exported by bats
      [ "$output" = "Bats $BATS_VERSION" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -ne 0 ]
      case $output in
        "Bats "*) false ;;
        *) ;;
      esac
      if [ "$pipefail" = yes ]; then
        [ "$status" -eq 1 ]
        [ "$output" = "bats: this program needs to be run by Bash >= 3.2" ]
      fi
      ;;
    *) false ;;
  esac
}
