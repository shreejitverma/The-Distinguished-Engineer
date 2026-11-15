#!/usr/bin/env bats
# Tests for hold-space.sh: the sed pattern space and hold space.
# The shell under test never changes which sed runs, so implementation-specific results
# branch on sed_flavor (GNU, BSD and macOS, BusyBox), not on lab_shell_family.

setup() {
  load ../lib/lab
  HS="$BATS_TEST_DIRNAME/hold-space.sh"
}

# sed_flavor: print gnu, bsd, busybox, uutils, or unknown for the sed on PATH.
# BusyBox answers --version with "This is not GNU sed version 4.0", so test it before GNU.
sed_flavor() {
  local out
  out=$(sed --version 2>&1 </dev/null) || true
  case $out in
    *"not GNU sed"*) echo busybox ;;
    *"GNU sed"*) echo gnu ;;
    *"usage: sed script"*) echo bsd ;;
    *uutils*) echo uutils ;;
    *) echo unknown ;;
  esac
}

@test "two-buffer idioms give the POSIX results with every sed" {
  run_lab "$HS" idioms
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "tac: 3|2|1" ]
  [ "${lines[1]}" = "double: a||b|" ]
  [ "${lines[2]}" = "commas: a,b,c" ]
  [ "${lines[3]}" = "pairs: 1 2|3 4|5" ]
  [ "${lines[4]}" = "tail2: 4|5" ]
  [ "${lines[5]}" = "uniq: a|b|c|a" ]
  [ "${lines[6]}" = "before: b|d" ]
  [ "${lines[7]}" = "odd: 1|3|5" ]
  [ "${#lines[@]}" -eq 8 ]
}

@test "x on line 1 swaps in the empty hold space and the last line stays held" {
  run_lab "$HS" firstx
  [ "$status" -eq 0 ]
  # newline (empty line 1), "a", newline; "b" is left in the hold space and never printed
  [ "$output" = "0a 61 0a" ]
}

@test "D restarts the cycle without reading input, so the line number does not advance" {
  run_lab "$HS" restart
  [ "$status" -eq 0 ]
  # "=" prints 3 twice: the second cycle starts from D's leftover "3", not from a new read
  [ "$output" = "2|1|3|2|3|3" ]
}

@test "N on the last line: GNU and BusyBox print, BSD and POSIXLY_CORRECT GNU do not" {
  run_lab "$HS" nlast
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu)
      [ "${lines[0]}" = "N: a|b|c" ]
      [ "${lines[1]}" = "N posixly: a|b" ]
      ;;
    bsd)
      [ "${lines[0]}" = "N: a|b" ]
      [ "${lines[1]}" = "N posixly: a|b" ]
      ;;
    busybox)
      [ "${lines[0]}" = "N: a|b|c" ]
      [ "${lines[1]}" = "N posixly: a|b|c" ]
      ;;
    *) false ;;
  esac
  [ "${lines[2]}" = "guarded: a|b|c" ]
}

@test "after an unterminated last line, x and G write a newline in GNU and BusyBox but not BSD" {
  run_lab "$HS" noeol
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | busybox)
      [ "${lines[0]}" = "x: 0a 61 0a" ]
      [ "${lines[1]}" = "G: 61 0a 0a" ]
      ;;
    bsd)
      [ "${lines[0]}" = "x: 0a 61" ]
      [ "${lines[1]}" = "G: 61 0a" ]
      ;;
    *) false ;;
  esac
}

@test "a NUL byte stays inside the line in GNU and BSD but ends the line in BusyBox" {
  run_lab "$HS" nul
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd)
      [ "${lines[0]}" = "lines: 1" ]
      [ "${lines[1]}" = "G: 61 00 62 0a 0a" ]
      ;;
    busybox)
      [ "${lines[0]}" = "lines: 2" ]
      [ "${lines[1]}" = "G: 61 0a 0a 62 0a 0a" ]
      ;;
    *) false ;;
  esac
}

@test "N reads a line, so it resets the t flag in GNU and BSD but not in BusyBox" {
  run_lab "$HS" tflag
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd) [ "$output" = "A|b no" ] ;;
    busybox) [ "$output" = "A|b yes" ] ;;
    *) false ;;
  esac
}

@test "the hold space spans file operands, and -i clears it per file except in BusyBox" {
  run_lab "$HS" files "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "stream: 0a 31 0a" ]
  [ "${lines[1]}" = "in-place f1: 0a" ]
  case $(sed_flavor) in
    gnu | bsd) [ "${lines[2]}" = "in-place f2: 0a" ] ;;
    busybox) [ "${lines[2]}" = "in-place f2: 31 0a" ] ;;
    *) false ;;
  esac
}

@test "the hold space holds 1001999 bytes, far past the POSIX minimum of 8192" {
  run_lab "$HS" big "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "bytes: 1002000" ]
  [ "${lines[1]}" = "lines: 1" ]
}

@test "an unknown mode fails with status 2 and a usage line" {
  run_lab "$HS" bogus
  [ "$status" -eq 2 ]
  [[ $output == usage:* ]]
}
