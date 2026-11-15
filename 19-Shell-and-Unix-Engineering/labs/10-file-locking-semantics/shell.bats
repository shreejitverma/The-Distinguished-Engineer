#!/usr/bin/env bats
# File locking as a shell script sees it: which process holds the open file description,
# which shells hide an exec'd descriptor from children, and what survives a crash.
# lockprobe stands in for flock(1), which macOS does not ship: "lockfd FD KIND" locks an
# inherited descriptor and exits; "test FILE KIND" opens FILE anew and reports free or busy.

setup() {
  load ../lib/lab
  PROBE=$BATS_TEST_TMPDIR/lockprobe
  "${CC:-cc}" -std=c99 -Wall -Wextra -Werror -O2 -o "$PROBE" "$BATS_TEST_DIRNAME/lockprobe.c"
  F=$BATS_TEST_TMPDIR/lockfile
  : >"$F"
  WITH_LOCK=$BATS_TEST_DIRNAME/with-lock.sh
}

@test "a lock a child takes on a group's fd 9 stays held by the shell's open" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '{ "$1" lockfd 9 flock; "$1" test "$2" flock; } 9>>"$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "locked
busy" ]
}

@test "exec 9>file: ksh93 and mksh hide fd 9 from external commands" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'exec 9>>"$2"; "$1" lockfd 9 flock; echo "status $?"; "$1" test "$2" flock' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | posh | dash)
      [ "$output" = "locked
status 0
busy" ]
      ;;
    ksh93 | mksh) # exec sets FD_CLOEXEC on descriptors above 2 (mksh: unless in posix or sh mode)
      [ "$output" = "EBADF
status 2
free" ]
      ;;
    *) false ;;
  esac
}

@test "an fcntl lock dies with the helper that took it; an OFD lock stays with the open" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '{ "$1" lockfd 9 fcntl; "$1" test "$2" fcntl; "$1" lockfd 9 ofd; "$1" test "$2" ofd; } 9>>"$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "locked
free
locked
busy" ]
}

@test "a background child that inherits fd 9 keeps the lock after the shell lets go" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '( "$1" lockfd 9 flock; /bin/sleep 2 >/dev/null 2>&1 & ) 9>>"$2"; "$1" test "$2" flock; /bin/sleep 3; "$1" test "$2" flock' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "locked
busy
free" ]
}

@test "closing fd 9 for the background child releases the lock with the shell" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '( "$1" lockfd 9 flock; /bin/sleep 2 9>&- >/dev/null 2>&1 & ) 9>>"$2"; "$1" test "$2" flock' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "locked
free" ]
}

@test "SIGKILL releases a kernel lock" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '{ "$1" lockfd 9 flock && kill -9 $$; } 9>>"$2"' x "$PROBE" "$F"
  [ "$status" -eq 137 ]
  [ "$output" = locked ]
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" test "$2" flock' x "$PROBE" "$F"
  [ "$output" = free ]
}

@test "SIGKILL leaves a mkdir lock and a noclobber lock file stale" {
  D=$BATS_TEST_TMPDIR/lock.d
  N=$BATS_TEST_TMPDIR/lock.noclobber
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'mkdir "$1" && kill -9 $$' x "$D"
  [ "$status" -eq 137 ]
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '(set -C; : >"$1") 2>/dev/null && kill -9 $$' x "$N"
  [ "$status" -eq 137 ]
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'if mkdir "$1" 2>/dev/null; then echo acquired; else echo refused; fi
    if (set -C; : >"$2") 2>/dev/null; then echo acquired; else echo refused; fi' x "$D" "$N"
  [ "$output" = "refused
refused" ]
}

@test "deleting a held lock file lets a second process lock a new inode" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '{ "$1" lockfd 9 flock; rm -f "$2"; : >"$2"; "$1" test "$2" flock; } 9>>"$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "locked
free" ]
}

@test "zsh zsystem flock is an fcntl lock: any redirection to the file drops it" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'if zmodload zsh/system 2>/dev/null; then zsystem flock "$2"; "$1" test "$2" fcntl; : >>"$2"; "$1" test "$2" fcntl; else echo none; fi' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh | zsh-sh)
      [ "$output" = "busy
free" ]
      ;;
    bash | yash | busybox | posh | dash | ksh93 | mksh) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "with-lock.sh runs the command, propagates its status, and releases" {
  L=$BATS_TEST_TMPDIR/job.lock
  run_lab "$WITH_LOCK" "$L" sh -c 'echo inside; exit 7'
  [ "$status" -eq 7 ]
  [ "$output" = inside ]
  [ ! -e "$L" ]
}

@test "with-lock.sh refuses a second holder while the first is alive" {
  L=$BATS_TEST_TMPDIR/job.lock
  run_lab "$WITH_LOCK" "$L" "$WITH_LOCK" "$L" echo never
  [ "$status" -eq 75 ]
  case $output in
    "with-lock: $L held by pid "[0-9]*) true ;;
    *) false ;;
  esac
  [ ! -e "$L" ]
}

@test "with-lock.sh reports a lock whose owner was SIGKILLed as stale" {
  L=$BATS_TEST_TMPDIR/job.lock
  # shellcheck disable=SC2016 # $PPID belongs to the inner sh: the with-lock.sh process
  run_lab "$WITH_LOCK" "$L" sh -c 'kill -9 $PPID'
  [ "$status" -eq 137 ]
  [ -d "$L" ]
  run_lab "$WITH_LOCK" "$L" echo never
  [ "$status" -eq 76 ]
  case $output in
    "with-lock: $L is stale (pid "[0-9]*" is gone); remove it by hand") true ;;
    *) false ;;
  esac
}

@test "with-lock.sh releases the lock when SIGTERM ends it" {
  L=$BATS_TEST_TMPDIR/job.lock
  # shellcheck disable=SC2016 # $PPID belongs to the inner sh: the with-lock.sh process
  run_lab "$WITH_LOCK" "$L" sh -c 'kill -TERM $PPID; /bin/sleep 1'
  [ "$status" -eq 143 ]
  [ ! -e "$L" ]
}
