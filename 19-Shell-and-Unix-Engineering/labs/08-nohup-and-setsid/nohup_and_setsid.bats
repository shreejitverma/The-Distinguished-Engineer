#!/usr/bin/env bats
# nohup and setsid: SIGHUP disposition, nohup.out, exit statuses, terminal hangup,
# controlling terminals, and setsid(1)'s fork rule, under the shell under test.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME/nohup_lab.sh"
  PROBE="$BATS_TEST_TMPDIR/hup_probe"
  WORK=$(mktemp -d "$BATS_TEST_TMPDIR/work.XXXXXX")
  "${CC:-cc}" -O2 -Wall -Wextra -pedantic "$BATS_TEST_DIRNAME/hup_probe.c" -o "$PROBE"
}

teardown() {
  local f pid
  for f in "$WORK"/*.pid; do
    [ -f "$f" ] || continue
    pid=$(cat "$f") || continue
    kill -KILL "$pid" 2>/dev/null || true
  done
}

# nohup_flavor: the implementation behind nohup: gnu, uutils, busybox, bsd, or unknown.
# The shell under test never changes which nohup runs, so nohup tests branch on this.
nohup_flavor() {
  local out
  out=$(nohup --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *)
      out=$(nohup --help 2>&1 </dev/null) || true
      case $out in
        *BusyBox*) echo busybox ;;
        *"usage: nohup [--] utility"*) echo bsd ;;
        *) echo unknown ;;
      esac
      ;;
  esac
}

# setsid_flavor: the implementation behind setsid(1): util-linux, busybox, none, or unknown.
setsid_flavor() {
  local out
  if ! command -v setsid >/dev/null 2>&1; then
    echo none
    return
  fi
  out=$(setsid --version 2>&1 </dev/null) || true
  case $out in
    *util-linux*) echo util-linux ;;
    *)
      out=$(setsid --help 2>&1 </dev/null) || true
      case $out in
        *BusyBox*) echo busybox ;;
        *) echo unknown ;;
      esac
      ;;
  esac
}

@test "nohup execs the utility with SIGHUP ignored and leaves non-terminal output alone" {
  run_lab "$LAB" hup-disposition "$PROBE" "$WORK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "plain: SIGHUP=default" ]
  [ "${lines[1]}" = "nohup: SIGHUP=ignored" ]
  [ "${lines[2]}" = "nohup.out: absent" ]
  [ "${#lines[@]}" -eq 3 ]
}

@test "nohup passes the utility's status through and reports 126 and 127 for exec failures" {
  run_lab "$LAB" exit-status "$PROBE" "$WORK"
  [ "$status" -eq 0 ]
  [ "$output" = "utility=3 noexec=126 missing=127" ]
}

@test "nohup's own failure is 125 in GNU and uutils unless POSIXLY_CORRECT, 127 in BSD and BusyBox" {
  run_lab "$LAB" internal-failure "$PROBE" "$WORK"
  [ "$status" -eq 0 ]
  case $(nohup_flavor) in
    gnu | uutils) [ "$output" = "no-operand=125 posixly-correct=127" ] ;;
    bsd | busybox) [ "$output" = "no-operand=127 posixly-correct=127" ] ;;
    *) false ;;
  esac
}

@test "nohup on a terminal appends to a 0600 nohup.out, joins stderr to it, and handles stdin per implementation" {
  run_lab "$LAB" nohup-on-tty "$PROBE" "$WORK"
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "exit=0" ]
  [ "${lines[2]}" = "mode=-rw-------" ]
  case $(nohup_flavor) in
    gnu)
      [ "${lines[0]}" = "nohup: ignoring input and appending output to 'nohup.out'" ]
      [ "${lines[3]}" = "nohup.out: fd0=unreadable fd1=regular fd2=same-as-fd1" ]
      ;;
    uutils)
      [ "${lines[0]}" = "nohup: ignoring input and appending output to 'nohup.out'" ]
      [ "${lines[3]}" = "nohup.out: fd0=unreadable fd1=regular fd2=same-as-fd1" ]
      ;;
    busybox)
      [ "${lines[0]}" = "nohup: appending output to nohup.out" ]
      [ "${lines[3]}" = "nohup.out: fd0=readable fd1=regular fd2=same-as-fd1" ]
      ;;
    bsd)
      [ "${lines[0]}" = "appending output to nohup.out" ]
      [ "${lines[3]}" = "nohup.out: fd0=tty fd1=regular fd2=same-as-fd1" ]
      ;;
    *) false ;;
  esac
}

@test "a terminal hangup kills a plain background job but not one under nohup or in a new session" {
  run_lab "$LAB" hangup "$PROBE" "$WORK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "READY" ]
  [ "${lines[1]}" = "hangup-sent" ]
  [ "${lines[2]}" = "leader=gone" ]
  [ "${lines[3]}" = "plain=dead" ]
  [ "${lines[4]}" = "nohup=alive" ]
  [ "${lines[5]}" = "newsession=alive" ]
}

@test "nohup keeps the controlling terminal; a new session has none" {
  run_lab "$LAB" ctty "$PROBE" "$WORK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "exit=0" ]
  [ "${lines[1]}" = "plain: leader=0 ctty=yes" ]
  [ "${lines[2]}" = "nohup: leader=0 ctty=yes" ]
  [ "${lines[3]}" = "newsession: leader=1 ctty=no" ]
}

@test "setsid execs in place, without forking, when its caller does not lead a process group" {
  local tool
  case $(setsid_flavor) in
    util-linux | busybox) tool=(setsid) ;;
    none)
      # No setsid(1) (macOS): the probe's newsession mode is the same algorithm.
      run_lab_c 'command -v setsid'
      [ "$status" -ne 0 ]
      tool=("$PROBE" newsession)
      ;;
    *) false ;;
  esac
  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c 'echo "shell=$$"; "$@" ids; :' -- "${tool[@]}" "$PROBE"
  [ "$status" -eq 0 ]
  [[ "${lines[1]}" =~ ^pid=([0-9]+)\ ppid=([0-9]+)\ pgid=([0-9]+)\ sid=([0-9]+)$ ]]
  local pid=${BASH_REMATCH[1]} ppid=${BASH_REMATCH[2]} pgid=${BASH_REMATCH[3]} sid=${BASH_REMATCH[4]}
  # The shell forked setsid, and setsid exec'd the probe in the same process.
  [ "$ppid" = "${lines[0]#shell=}" ]
  [ "$pid" = "$pgid" ]
  [ "$pid" = "$sid" ]
}

@test "setsid forks and returns at once when its caller leads a process group" {
  run_lab "$LAB" setsid-leader "$PROBE" "$WORK"
  [ "$status" -eq 0 ]
  case $(setsid_flavor) in
    util-linux | busybox) [ "${lines[0]}" = "tool=setsid" ] ;;
    none) [ "${lines[0]}" = "tool=probe" ] ;;
    *) false ;;
  esac
  [ "${lines[1]}" = "status=0 returned_early=yes child_leads_session=yes" ]
}

@test "util-linux setsid -w returns the child's exit status and the bare signal number for a kill" {
  run_lab "$LAB" setsid-wait "$PROBE" "$WORK"
  [ "$status" -eq 0 ]
  case $(setsid_flavor) in
    util-linux) [ "$output" = "exit=7 killed=15" ] ;;
    busybox) [ "$output" = "exit=1 killed=1" ] ;;
    none) [ "$output" = "setsid: absent" ] ;;
    *) false ;;
  esac
}

@test "a non-interactive shell started under nohup cannot trap SIGHUP, except zsh" {
  run_lab "$LAB" trap-ignored "$PROBE" "$WORK"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh | zsh-sh)
      [ "${lines[0]}" = "caught" ]
      [ "${lines[1]}" = "survived" ]
      [ "${#lines[@]}" -eq 2 ]
      ;;
    bash | dash | ksh93 | mksh | posh | yash | busybox)
      [ "$output" = "survived" ]
      ;;
    *) false ;;
  esac
}
