#!/usr/bin/env bats
# Tests for join-comm.sh. The shell under test only drives the probe; join and comm are external
# utilities, so implementation-specific behavior branches on util_flavor, never on the shell family.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/join-comm.sh"
}

# util_flavor NAME: print which implementation of NAME (join or comm) is first on PATH:
# gnu, uutils, busybox, bsd, missing, or unknown.
util_flavor() {
  local out
  if ! command -v "$1" >/dev/null 2>&1; then
    echo missing
    return
  fi
  out=$("$1" --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"illegal option"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

@test "demo: inner, left, full outer, and anti-join, then comm set operations" {
  run_lab "$PROBE" demo "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  expected='--- inner join
c1,Alice,o-11
c1,Alice,o-12
c2,Bob,o-17
c3,Carol,o-30
--- left outer join, -o 0 keeps the key of unpaired lines
c1,Alice,o-11
c1,Alice,o-12
c2,Bob,o-17
c3,Carol,o-30
c4,Dan,NONE
--- full outer join
c1,Alice,o-11
c1,Alice,o-12
c2,Bob,o-17
c3,Carol,o-30
c4,Dan,NONE
c9,NONE,o-99
--- anti-join: users with no order
c4,Dan
--- comm -12: ids in both
c1
c2
c3
--- comm -3: ids in exactly one file (file 2 indented)
c4
|c9'
  [ "$output" = "$expected" ]
}

@test "cross: a key repeated 3 times and 4 times yields the 12-line cross product in file-1-major order" {
  run_lab "$PROBE" cross "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = 12 ]
  [ "${lines[1]}" = "k a1 b1" ]
  [ "${lines[2]}" = "k a3 b4" ]
}

@test "sortkey: whole-line sort order loses a match; key sort finds all three" {
  command -v join >/dev/null || skip "no join utility (BusyBox ships comm only)"
  run_lab "$PROBE" sortkey "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "line-sorted: 10:y 1:x 2:z " ]
  [ "${lines[1]}" = "key-sorted:  1:x 10:y 2:z " ]
  # The 1:x / 1:p pair is missing: join passed key 1 while file 1 was still at 10.
  [ "${lines[2]}" = "10:y:q" ]
  [ "${lines[3]}" = "2:z:r" ]
  case $(util_flavor join) in
    gnu) [ "${lines[4]}" = "mixed: status=1 stderr=yes" ] ;; # check_order warns once, exit 1
    bsd) [ "${lines[4]}" = "mixed: status=0 stderr=no" ] ;;  # BSD join never checks order
    *) false ;;
  esac
  [ "${lines[5]}" = "1:x:p" ]
  [ "${lines[6]}" = "10:y:q" ]
  [ "${lines[7]}" = "2:z:r" ]
  [ "${lines[8]}" = "keyed: status=0 stderr=no" ]
  [ "${#lines[@]}" -eq 9 ]
}

@test "blanks: default blank fields collapse, -t fields do not, and empty join fields pair" {
  run_lab "$PROBE" blanks "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "k 1 2" ]
  [ "${lines[1]}" = "k|EMPTY|2" ]
  [ "${lines[2]}" = "<empty>" ]
  [ "${lines[3]}" = "m 1 2" ]
  [ "${#lines[@]}" -eq 4 ]
}

@test "multiset: comm pairs occurrences, so a duplicate line lands in column 1 or 2" {
  run_lab "$PROBE" multiset "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  expected='apple
||banana
banana
||cherry
|cherry
|date
--- -23 (only in file 1)
apple
banana'
  [ "$output" = "$expected" ]
}

@test "disorder: unsorted comm input reports line a as unique to both files" {
  run_lab "$PROBE" disorder "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "|a" ]
  [ "${lines[1]}" = "b" ]
  [ "${lines[2]}" = "a" ]
  [ "${lines[3]}" = "||c" ]
  case $(util_flavor comm) in
    gnu) [ "${lines[4]}" = "comm: status=1 stderr=yes" ] ;;
    bsd | busybox) [ "${lines[4]}" = "comm: status=0 stderr=no" ] ;;
    *) false ;;
  esac
}

@test "crlf: a carriage return makes otherwise equal lines differ until it is stripped" {
  run_lab "$PROBE" crlf "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "common-raw=0" ]
  [ "${lines[1]}" = "common-fixed=2" ]
}

@test "ext-join: -a with -v, -e on an empty field, and -i differ by implementation" {
  command -v join >/dev/null || skip "no join utility (BusyBox ships comm only)"
  run_lab "$PROBE" ext-join "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(util_flavor join) in
    gnu)
      [ "${lines[0]}" = "a 1" ]
      [ "${lines[1]}" = "c 4" ]
      [ "${lines[2]}" = "av: status=0 stderr=no" ]
      [ "${lines[3]}" = "K,NA,x,y" ]
      [ "${lines[4]}" = "fill: status=0 stderr=no" ]
      [ "${lines[5]}" = "A 1 2" ]
      [ "${lines[6]}" = "joini: status=0 stderr=no" ]
      ;;
    bsd)
      # BSD join rejects -a with -v ("mutually exclusive") and has no -i.
      [ "${lines[0]}" = "av: status=1 stderr=yes" ]
      case $(uname -s) in
        Darwin) [ "${lines[1]}" = "K,NA,x,y" ] ;; # text_cmds outfield fills in UNIX2003 mode
        FreeBSD) [ "${lines[1]}" = "K,,x,y" ] ;;  # FreeBSD outfield returns on an empty field
        *) false ;;
      esac
      [ "${lines[2]}" = "fill: status=0 stderr=no" ]
      [ "${lines[3]}" = "joini: status=1 stderr=yes" ]
      ;;
    *) false ;;
  esac
}

@test "ext-comm: -i exists only in BSD comm, --total only in GNU comm" {
  run_lab "$PROBE" ext-comm "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(util_flavor comm) in
    gnu)
      [ "${lines[0]}" = "commi: status=1 stderr=yes" ]
      [ "${lines[1]}" = "a 1" ]
      [ "${lines[2]}" = "b 2" ]
      [ "${lines[3]}" = "|b 3" ]
      [ "${lines[4]}" = "|c 4" ]
      [ "${lines[5]}" = "2|2|0|total" ]
      [ "${lines[6]}" = "total: status=0 stderr=no" ]
      ;;
    bsd)
      [ "${lines[0]}" = "||A" ]
      [ "${lines[1]}" = "commi: status=0 stderr=no" ]
      [ "${lines[2]}" = "total: status=1 stderr=yes" ]
      ;;
    busybox)
      [ "${lines[0]}" = "commi: status=1 stderr=yes" ]
      [ "${lines[1]}" = "total: status=1 stderr=yes" ]
      ;;
    *) false ;;
  esac
}

@test "an unknown mode is rejected with status 2" {
  run_lab "$PROBE" no-such-mode "$BATS_TEST_TMPDIR"
  [ "$status" -eq 2 ]
  [ "$output" = "join-comm.sh: unknown mode: no-such-mode" ]
}

@test "process substitution feeds comm without temp files only in bash, zsh, ksh93, and BusyBox ash" {
  local family version posix_off old_bash
  family=$(lab_shell_family)
  version=$(lab_shell_version)
  run_lab_c '[ -o posix ]'
  posix_off=$status
  case $version in
    [34].* | 5.0.*) old_bash=yes ;;
    *) old_bash=no ;;
  esac
  if [ "$family" = bash ] && [ "$posix_off" -eq 0 ] && [ "$old_bash" = yes ]; then
    # Before bash-5.1 POSIX mode disables process substitution (bash CHANGES, bash-5.1-alpha).
    family=bash-posix-old
  fi
  run_lab_c 'LC_ALL=C comm -12 <(printf "a\nb\n") <(printf "b\n")'
  case $family in
    bash | zsh | zsh-sh | ksh93 | busybox)
      # BusyBox ash 1.37.0 has <(...) when built with ASH_BASH_COMPAT (shell/ash.c BASH_PROCESS_SUBST),
      # as the Alpine and Debian builds are.
      [ "$status" -eq 0 ]
      [ "$output" = b ]
      ;;
    bash-posix-old | dash | posh | mksh)
      # No process substitution: "<(" is a syntax error.
      [ "$status" -ne 0 ]
      [ "$output" != b ]
      ;;
    yash)
      # yash <(cmd) is process redirection and expands to nothing, so comm gets no operands.
      [ "$status" -ne 0 ]
      [ "$output" != b ]
      ;;
    *) false ;;
  esac
}
