#!/usr/bin/env bats
# Tests for grep-probe.sh: exit status, pattern lists, binary detection, -m and the
# file offset, recursion and symlinks, input-is-output, option permutation, and escapes.
# The shell under test never changes which grep runs, so implementation-specific
# assertions branch on the grep flavor the probe detects (gnu, bsd, busybox).

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/grep-probe.sh"
}

# probe MODE: run the probe in a fresh scratch directory under the shell under test.
probe() {
  local d
  d=$(mktemp -d "$BATS_TEST_TMPDIR/probe.XXXXXX")
  run_lab "$PROBE" "$1" "$d"
}

# detect_flavor: set FLAVOR to the grep implementation the shell under test runs.
detect_flavor() {
  probe flavor
  [ "$status" -eq 0 ]
  FLAVOR=${output#flavor=}
}

@test "the probe recognizes the grep implementation" {
  detect_flavor
  case $FLAVOR in
    gnu | bsd | busybox) true ;;
    *) false ;;
  esac
}

@test "exit status is 0, 1, or 2, and -q turns an error plus a match into 0" {
  probe exit
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "match=0" ]
  [ "${lines[1]}" = "nomatch=1" ]
  [ "${lines[2]}" = "missing_then_match=2" ]
  [ "${lines[3]}" = "q_missing_then_match=0" ]
  [ "${lines[4]}" = "s_missing_then_match=2 s_stderr_bytes=0" ]
  [ "${lines[5]}" = "count_zero=<0> status=1" ]
}

@test "a newline splits a pattern list and an empty pattern matches every line" {
  detect_flavor
  probe patterns
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "newline_list=2" ]
  [ "${lines[1]}" = "empty_pattern=3" ]
  [ "${lines[2]}" = "file_with_blank_line=3" ]
  case $FLAVOR in
    gnu) [ "${lines[3]}" = "empty_pattern_file=<> status=1" ] ;; # exits before reading input
    bsd | busybox) [ "${lines[3]}" = "empty_pattern_file=<0> status=1" ] ;;
    *) false ;;
  esac
  [ "${lines[4]}" = "dot_bre=2 dot_fixed=1" ]
}

@test "binary detection depends on the implementation and on where the NUL falls" {
  detect_flavor
  probe binary
  [ "$status" -eq 0 ]
  [ "${lines[4]}" = "nul_offset: near=40006 far=200006" ]
  [ "${lines[3]}" = "a_small: out=<match> err=<> rc=0" ]
  case $FLAVOR in
    gnu)
      # GNU checks every 96 KiB read for NULs and reports on stderr.
      [ "${lines[0]}" = "small.bin: out=<> err=<grep: small.bin: binary file matches> rc=0" ]
      [ "${lines[1]}" = "near.bin: out=<> err=<grep: near.bin: binary file matches> rc=0" ]
      [ "${lines[2]}" = "far.bin: out=<match> err=<> rc=0" ]
      ;;
    bsd)
      # BSD checks only the first 32 KiB read and reports on stdout.
      [ "${lines[0]}" = "small.bin: out=<Binary file small.bin matches> err=<> rc=0" ]
      [ "${lines[1]}" = "near.bin: out=<match> err=<> rc=0" ]
      [ "${lines[2]}" = "far.bin: out=<match> err=<> rc=0" ]
      ;;
    busybox)
      # BusyBox has no binary detection: it always acts as -a.
      [ "${lines[0]}" = "small.bin: out=<match> err=<> rc=0" ]
      [ "${lines[1]}" = "near.bin: out=<match> err=<> rc=0" ]
      [ "${lines[2]}" = "far.bin: out=<match> err=<> rc=0" ]
      ;;
    *) false ;;
  esac
}

@test "-m 1 on a seekable stdin leaves the offset after the last selected line in GNU" {
  detect_flavor
  probe maxcount
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu) [ "$output" = "grep=<x1> rest=<b,x2,c,>" ] ;;     # finalize_input lseeks back
    busybox) [ "$output" = "grep=<x1> rest=<b,x2,c,>" ] ;; # musl exit() repositions stdin
    bsd) [ "$output" = "grep=<x1> rest=<>" ] ;;            # the 32 KiB read consumed it all
    *) false ;;
  esac
}

@test "recursive search follows symlinks differently in each implementation" {
  detect_flavor
  probe recurse
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu)
      [ "${lines[0]}" = "r_tree=<tree/sub/real:hit,>" ]
      [ "${lines[1]}" = "r_cmdline_link=<tlink/sub/real:hit,>" ]
      [ "${lines[2]}" = "R_tree=<tree/dirlink/f:hit,tree/sub/link:hit,tree/sub/real:hit,>" ]
      ;;
    bsd)
      [ "${lines[0]}" = "r_tree=<tree/sub/real:hit,>" ]
      [ "${lines[1]}" = "r_cmdline_link=<>" ]
      [ "${lines[2]}" = "R_tree=<tree/sub/real:hit,>" ]
      ;;
    busybox)
      [ "${lines[0]}" = "r_tree=<tree/sub/link:hit,tree/sub/real:hit,>" ]
      [ "${lines[1]}" = "r_cmdline_link=<tlink/sub/link:hit,tlink/sub/real:hit,>" ]
      [ "${lines[2]}" = "R_tree=<tree/dirlink/f:hit,tree/sub/link:hit,tree/sub/real:hit,>" ]
      ;;
    *) false ;;
  esac
}

@test "appending grep output to its own input: GNU refuses, the others copy" {
  detect_flavor
  probe selfout
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu) [ "$output" = "rc=2 lines=1 err=<grep: s.txt: input file is also the output>" ] ;;
    bsd | busybox) [ "$output" = "rc=0 lines=2 err=<>" ] ;;
    *) false ;;
  esac
}

@test "a file named -v in a bare glob becomes an option and hides the match" {
  detect_flavor
  probe dashfile
  [ "$status" -eq 0 ]
  # POSIX getopt would stop at the pattern operand; all three permute argv instead.
  case $FLAVOR in
    gnu | bsd | busybox) [ "${lines[0]}" = "star=<> status=1" ] ;;
    *) false ;;
  esac
  [ "${lines[1]}" = "dashdash=<data:secret> status=0" ]
  [ "${lines[2]}" = "dot_slash=<./data:secret> status=0" ]
}

@test "backslash-d is a digit class in BSD and musl regex but a stray backslash in GNU" {
  detect_flavor
  probe escape
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu) [ "$output" = 'backslash_d=<ad,> err=<grep: warning: stray \ before d>' ] ;;
    bsd | busybox) [ "$output" = "backslash_d=<a1,> err=<>" ] ;;
    *) false ;;
  esac
}
