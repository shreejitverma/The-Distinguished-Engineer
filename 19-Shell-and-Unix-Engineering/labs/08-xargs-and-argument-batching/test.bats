#!/usr/bin/env bats
# Tests for xargs argument batching, delimiter parsing, and exit semantics.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/batch_probe.sh"
}

xargs_flavor() {
  local out
  out=$(xargs --version 2>&1 </dev/null || true)
  case "$out" in
    *"GNU findutils"* | *"GNU xargs"*) printf 'gnu\n' ;;
    *"BusyBox"*) printf 'busybox\n' ;;
    *)
      # shellcheck disable=SC2312 # a failed uname only means the system is not Darwin or BSD
      if uname -s | grep -qi "darwin\|bsd"; then
        printf 'bsd\n'
      else
        printf 'unknown\n'
      fi
      ;;
  esac
}

@test "xargs -n batches arguments into fixed chunks" {
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c 'printf "%s\n" 1 2 3 4 5 6 7 | xargs -n 3 "$1" count' sh "$PROBE"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "3 args: <1> <2> <3>" ]
  [ "${lines[1]}" = "3 args: <4> <5> <6>" ]
  [ "${lines[2]}" = "1 args: <7>" ]
}

@test "xargs -0 handles quotes spaces and hyphens safely" {
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c 'printf "%s\0" "file1" "file with spaces" "O'\''Reilly" "-delete" | xargs -0 "$1" count --' sh "$PROBE"
  [ "$status" -eq 0 ]
  [ "$output" = "5 args: <--> <file1> <file with spaces> <O'Reilly> <-delete>" ]
}

@test "unquoted single quote triggers unterminated quote error" {
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c 'printf "valid\nO'\''Reilly.txt\nnever_reached\n" | xargs "$1" count 2>/dev/null' sh "$PROBE"
  [ "$status" -eq 1 ]
  [[ "$output" != *"never_reached"* ]]
}

@test "utility exit 255 triggers immediate abort of remaining input" {
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c 'printf "%s\n" a b c d e | xargs -n 1 "$1" abort_on_target c 2>/dev/null' sh "$PROBE"
  case $(xargs_flavor) in
    gnu | busybox) [ "$status" -eq 124 ] ;;
    bsd) [ "$status" -eq 1 ] ;;
    *) false ;;
  esac
  # Only a and b ran to completion; c aborted, so d and e never ran.
  [ "${#lines[@]}" -eq 2 ]
  [ "${lines[0]}" = "processed 1 items" ]
  [ "${lines[1]}" = "processed 1 items" ]
}

@test "empty input without -r runs once on GNU and BusyBox, zero times on BSD" {
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c 'printf "" | xargs "$1" count' sh "$PROBE"
  [ "$status" -eq 0 ]
  case $(xargs_flavor) in
    gnu | busybox) [ "$output" = "0 args:" ] ;;
    bsd) [ "$output" = "" ] ;;
    *) false ;;
  esac
}

@test "exit status 127 returned when utility is not found" {
  run_lab_c 'printf "foo\n" | xargs /nonexistent_cmd_xyz_bats_probe 2>/dev/null'
  [ "$status" -eq 127 ]
}

@test "exit status 126 returned when utility cannot be executed" {
  local noexec="$BATS_TEST_TMPDIR/noexec_probe"
  touch "$noexec"
  chmod -x "$noexec"
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c 'printf "foo\n" | xargs "$1" 2>/dev/null' sh "$noexec"
  [ "$status" -eq 126 ]
}

@test "exit status propagation when utility returns failure status 1" {
  run_lab_c 'printf "foo\n" | xargs sh -c "exit 1" 2>/dev/null'
  case $(xargs_flavor) in
    gnu | busybox) [ "$status" -eq 123 ] ;;
    bsd) [ "$status" -eq 1 ] ;;
    *) false ;;
  esac
}

@test "empty input with -r executes utility zero times" {
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c 'printf "" | xargs -r "$1" count' sh "$PROBE"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}

@test "xargs -L groups lines on GNU and BSD and is rejected by BusyBox" {
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c 'printf "a b\nc\nd e f\n" | xargs -L 2 "$1" count 2>/dev/null' sh "$PROBE"
  case $(xargs_flavor) in
    gnu | bsd)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "3 args: <a> <b> <c>" ]
      [ "${lines[1]}" = "3 args: <d> <e> <f>" ]
      ;;
    busybox) [ "$status" -ne 0 ] ;;
    *) false ;;
  esac
}

@test "xargs -E stops input at the logical EOF string" {
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c 'printf "a\nSTOP\nb\n" | xargs -E STOP "$1" count' sh "$PROBE"
  [ "$status" -eq 0 ]
  [ "$output" = "1 args: <a>" ]
}
