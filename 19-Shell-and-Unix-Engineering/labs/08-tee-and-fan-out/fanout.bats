#!/usr/bin/env bats
# Tests for fanout.sh and for shell-level fan-out (zsh MULTIOS, process substitution).
# tee behavior depends on which tee is on PATH (GNU, BSD and macOS, BusyBox, uutils),
# not on the shell, so utility tests branch on tee_flavor and shell tests on lab_shell_family.

setup() {
  load ../lib/lab
  FANOUT="$BATS_TEST_DIRNAME/fanout.sh"
}

# tee_flavor: print the implementation behind tee as gnu, uutils, busybox, bsd, or unknown.
tee_flavor() {
  local out
  out=$(cd "$BATS_TEST_TMPDIR" && tee --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"usage: tee [-ai]"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

@test "tee copies the whole stream to stdout and to every file operand" {
  run_lab "$FANOUT" copy "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "out: identical
a: identical
b: identical" ]
}

@test "tee truncates by default, appends with -a, and accepts -i" {
  run_lab "$FANOUT" append "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "f: new more " ]
}

@test "the operand - is a file named - except in BusyBox, where it is stdout" {
  run_lab "$FANOUT" dash "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(tee_flavor) in
    gnu | bsd | uutils) [ "$output" = "stdout copies: 1
file named -: yes" ] ;;
    busybox) [ "$output" = "stdout copies: 2
file named -: no" ] ;;
    *) false ;;
  esac
}

@test "an operand that cannot be opened fails the exit status but not the other outputs" {
  run_lab "$FANOUT" openfail "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "status: 1
ok: a
stdout: a
diagnostics: 1" ]
}

@test "tee empties its own input file before reading it" {
  run_lab "$FANOUT" clobber "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "f: 0 bytes
stdout: 0 bytes" ]
}

@test "a downstream reader that exits early kills tee and truncates the file operand" {
  run_lab "$FANOUT" sigpipe "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "f: truncated
tee: killed by a signal" ]
}

@test "with SIGPIPE ignored tee completes the file; only BSD tee diagnoses and fails" {
  run_lab "$FANOUT" ignored "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(tee_flavor) in
    gnu | busybox | uutils) [ "$output" = "f: complete
tee: exit 0
diagnostics: none" ] ;;
    bsd) [ "$output" = "f: complete
tee: exit 1
diagnostics: some" ] ;;
    *) false ;;
  esac
}

@test "tee -p keeps feeding files after a pipe breaks in GNU and uutils, and is rejected elsewhere" {
  run_lab "$FANOUT" pflag "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(tee_flavor) in
    gnu | uutils) [ "$output" = "f: complete
tee: exit 0" ] ;;
    bsd | busybox) [ "$output" = "f: absent
tee: exit 1" ] ;;
    *) false ;;
  esac
}

@test "the slowest consumer gates every other consumer of tee" {
  run_lab "$FANOUT" lockstep "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "fast consumer lines: 200000
fast consumer done before slow one read: no" ]
}

@test "tee into FIFOs fans one stream out to independent readers" {
  run_lab "$FANOUT" fifo "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "consumer 1 (lines): 200000
consumer 2 (lines ending in 0): 20000" ]
}

@test "two output redirections tee in native zsh (MULTIOS) and replace each other elsewhere" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '{ echo hi; } >"$1/a" >"$1/b"; printf "a=[%s] b=[%s]\n" "$(cat "$1/a")" "$(cat "$1/b")"' sh "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh) [ "$output" = "a=[hi] b=[hi]" ] ;;
    zsh-sh | bash | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = "a=[] b=[hi]" ] ;;
    *) false ;;
  esac
}

@test "tee >(cmd) is process substitution in bash, zsh and ksh93, and a syntax error in POSIX-only shells" {
  local family
  family=$(lab_shell_family)
  if [ "$family" = bash ]; then
    # bash before 5.1 rejects process substitution in POSIX mode (macOS /bin/sh is bash 3.2 in POSIX mode).
    # shellcheck disable=SC2016 # the expansions belong to the shell under test
    run_lab_c 'if [ -o posix ] && [ "${BASH_VERSINFO[0]}${BASH_VERSINFO[1]}" -lt 51 ]; then echo old-posix; else echo ok; fi'
    [ "$output" = old-posix ] && family=bash-old-posix
  fi
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'eval '\''printf "x\n" | tee >(cat >"$1/ps") >/dev/null'\''' sh "$BATS_TEST_TMPDIR"
  case $family in
    bash | zsh | zsh-sh | ksh93)
      [ "$status" -eq 0 ]
      # The shell does not wait for the substituted process, so poll for its output.
      lab_poll 5 grep -q x "$BATS_TEST_TMPDIR/ps"
      ;;
    yash)
      # yash >(cmd) is process redirection: it replaces tee's stdout, which >/dev/null then overrides.
      [ "$status" -eq 0 ]
      /bin/sleep 1
      [ ! -s "$BATS_TEST_TMPDIR/ps" ]
      ;;
    bash-old-posix | dash | mksh | posh | busybox)
      [ "$status" -ne 0 ]
      [ ! -e "$BATS_TEST_TMPDIR/ps" ]
      ;;
    *) false ;;
  esac
}
