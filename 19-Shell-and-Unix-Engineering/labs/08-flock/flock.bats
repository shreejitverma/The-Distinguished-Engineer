#!/usr/bin/env bats
# flock.bats: who owns a lock (flock(2) versus fcntl(2)), the shell idioms that hand a
# lock descriptor to a utility, and how flock(1) (util-linux, BusyBox) and lockf(1)
# (FreeBSD, macOS) differ.
# shellcheck disable=SC2016 # every run_lab_c string is expanded by the shell under test
# shellcheck disable=SC2030,SC2031 # HOLDER is per test; setup and teardown share it on purpose

setup() {
  load ../lib/lab
  LAB_DIR=$BATS_TEST_DIRNAME
  LOCK=$BATS_TEST_TMPDIR/job.lock
  HOLDER=""
  HOLDER_CHILD=""
}

teardown() {
  if [ -n "$HOLDER_CHILD" ]; then
    kill "$HOLDER_CHILD" 2>/dev/null || true
  fi
  if [ -n "$HOLDER" ]; then
    kill "$HOLDER" 2>/dev/null || true
    wait "$HOLDER" 2>/dev/null || true
  fi
}

build_probe() {
  PROBE=$BATS_TEST_TMPDIR/lockprobe
  "${CC:-cc}" -Wall -Wextra -Werror -O2 -o "$PROBE" "$LAB_DIR/lockprobe.c"
}

# lock_impl: which lock utility this host provides.
lock_impl() {
  if command -v flock >/dev/null 2>&1; then
    # shellcheck disable=SC2312 # a failing --version or --help only means the flock is not util-linux or BusyBox
    if flock --version 2>&1 | grep -q util-linux; then
      echo util-linux
    elif flock --help 2>&1 | grep -q BusyBox; then
      echo busybox
    else
      echo unknown-flock
    fi
  elif command -v lockf >/dev/null 2>&1; then
    echo bsd-lockf
  else
    echo none
  fi
}

# hold_lock SECONDS: hold an exclusive lock on $LOCK from a background utility.
hold_lock() {
  local marker=$BATS_TEST_TMPDIR/held
  local body='echo "$$" >"$1"; exec /bin/sleep "$2"'
  case $(lock_impl) in
    util-linux | busybox)
      flock "$LOCK" /bin/sh -c "$body" sh "$marker" "$1" >/dev/null 2>&1 3>&- &
      ;;
    bsd-lockf)
      lockf -k "$LOCK" /bin/sh -c "$body" sh "$marker" "$1" >/dev/null 2>&1 3>&- &
      ;;
    *)
      return 1
      ;;
  esac
  HOLDER=$!
  lab_poll 10 test -s "$marker"
  read -r HOLDER_CHILD <"$marker"
}

# --- flock(2) and fcntl(2) semantics, through a C probe -------------------------------

@test "two open() calls in one process get two flock locks that conflict" {
  build_probe
  run_lab_c '"$1" two-opens "$2"' inline "$PROBE" "$LOCK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "first-open LOCK_EX: granted" ]
  [ "${lines[1]}" = "second-open LOCK_EX: blocked" ]
}

@test "a dup()ed descriptor shares the flock lock and LOCK_UN through it releases it" {
  build_probe
  run_lab_c '"$1" dup "$2"' inline "$PROBE" "$LOCK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "original LOCK_EX: granted" ]
  [ "${lines[1]}" = "dup LOCK_EX: granted" ]
  [ "${lines[2]}" = "other open while held: blocked" ]
  [ "${lines[3]}" = "other open after LOCK_UN via dup: granted" ]
}

@test "closing an unrelated descriptor drops fcntl locks but not flock locks" {
  build_probe
  run_lab_c '"$1" posix-close "$2"' inline "$PROBE" "$LOCK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "fcntl lock after closing another fd: gone" ]
  [ "${lines[1]}" = "flock lock after closing another fd: held" ]
}

@test "a forked child shares the flock lock but does not own the parent's fcntl lock" {
  build_probe
  run_lab_c '"$1" inherit "$2"' inline "$PROBE" "$LOCK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "flock after parent close, child alive: blocked" ]
  [ "${lines[1]}" = "flock after child exit: granted" ]
  [ "${lines[2]}" = "fcntl F_SETLK by child on inherited fd: blocked" ]
}

@test "flock and fcntl locks are independent on Linux but conflict on macOS and FreeBSD" {
  build_probe
  run_lab_c '"$1" mix "$2"' inline "$PROBE" "$LOCK"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Linux)
      [ "${lines[0]}" = "fcntl F_SETLK while another process holds flock LOCK_EX: granted" ]
      [ "${lines[1]}" = "flock LOCK_NB by the process that holds the fcntl lock: granted" ]
      ;;
    Darwin | FreeBSD)
      [ "${lines[0]}" = "fcntl F_SETLK while another process holds flock LOCK_EX: blocked" ]
      [ "${lines[1]}" = "flock LOCK_NB by the process that holds the fcntl lock: blocked" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "classic fcntl locks never conflict within a process; OFD locks conflict per open" {
  build_probe
  run_lab_c '"$1" ofd "$2"' inline "$PROBE" "$LOCK"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Linux | Darwin)
      [ "${lines[0]}" = "F_SETLK via second open, same process: granted" ]
      [ "${lines[1]}" = "F_OFD_SETLK via second open, same process: blocked" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "F_SETLKW reports EDEADLK for a lock cycle while flock() blocks until a signal" {
  build_probe
  run_lab_c '"$1" deadlock "$2"' inline "$PROBE" "$LOCK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "fcntl F_SETLKW closing the cycle: EDEADLK" ]
  [ "${lines[1]}" = "flock LOCK_EX closing the cycle: EINTR after 2s alarm" ]
}

@test "bench reports a positive cost for an uncontended lock and unlock pair" {
  build_probe
  run_lab_c '"$1" bench "$2" 100000' inline "$PROBE" "$LOCK"
  [ "$status" -eq 0 ]
  [[ ${lines[0]} =~ ^"flock pair ns: "[1-9][0-9]*$ ]]
  [[ ${lines[1]} =~ ^"fcntl pair ns: "[1-9][0-9]*$ ]]
}

# --- the shell side: handing fd 9 to the lock utility ----------------------------------

@test "the ( ... ) 9>>lock form admits one holder at a time" {
  local out=$BATS_TEST_TMPDIR/holder.out
  lab_shell_words
  "${LAB_SHELL_WORDS[@]}" "$LAB_DIR/flock_demo.sh" singleton "$LOCK" >"$out" 2>&1 3>&- &
  HOLDER=$!
  lab_poll 10 grep -q acquired "$out"
  run_lab "$LAB_DIR/flock_demo.sh" singleton "$LOCK"
  [ "$status" -eq 75 ]
  [ "$output" = "busy" ]
  wait "$HOLDER"
  HOLDER=""
  # shellcheck disable=SC2312 # a failed read shows up as a mismatch
  [ "$(cat "$out")" = "$(printf 'acquired\nreleased')" ]
}

@test "exec 9>>lock then an external lock on fd 9 fails in ksh93 and mksh, which close fd 9 on exec" {
  run_lab "$LAB_DIR/flock_demo.sh" exec-form "$LOCK"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    ksh93 | mksh)
      [ "$output" = "not locked" ]
      ;;
    bash | zsh | zsh-sh | dash | yash | busybox | posh)
      [ "$output" = "locked" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "a background child started inside the critical section keeps holding the lock" {
  run_lab "$LAB_DIR/flock_demo.sh" inherit "$LOCK"
  [ "$status" -eq 0 ]
  [ "$output" = "busy" ]
}

@test "closing fd 9 in the background child with 9>&- releases the lock with the section; yash needs exec" {
  run_lab "$LAB_DIR/flock_demo.sh" inherit-closed "$LOCK"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    yash)
      # yash runs the job as a yash child holding a saved copy of fd 9 (as fd 100)
      [ "$output" = "busy" ]
      ;;
    *)
      [ "$output" = "free" ]
      ;;
  esac
  run_lab "$LAB_DIR/flock_demo.sh" inherit-closed-exec "$LOCK"
  [ "$status" -eq 0 ]
  [ "$output" = "free" ]
}

# --- the utilities: flock(1) from util-linux and BusyBox, lockf(1) from BSD ------------

@test "macOS ships lockf(1) but no flock(1); Linux images ship flock(1)" {
  run_lab_c 'command -v flock >/dev/null 2>&1 && echo flock; command -v lockf >/dev/null 2>&1 && echo lockf; :'
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Darwin)
      [ "$output" = "lockf" ]
      ;;
    Linux)
      [ "${lines[0]}" = "flock" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "a non-blocking request on a held lock fails: flock exits 1, lockf exits 75" {
  hold_lock 4
  case $(lock_impl) in
    util-linux | busybox)
      run_lab_c 'flock -n "$1" echo ran' inline "$LOCK"
      [ "$status" -eq 1 ]
      [ "$output" = "" ]
      ;;
    bsd-lockf)
      run_lab_c 'lockf -s -t 0 -k "$1" echo ran' inline "$LOCK"
      [ "$status" -eq 75 ]
      [ "$output" = "" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "util-linux -E chooses the conflict status; BusyBox and lockf reject the option" {
  case $(lock_impl) in
    util-linux)
      run_lab_c 'flock -n -E 42 "$1" echo ran' inline "$LOCK"
      [ "$status" -eq 0 ]
      [ "$output" = "ran" ]
      hold_lock 4
      run_lab_c 'flock -n -E 42 "$1" echo ran' inline "$LOCK"
      [ "$status" -eq 42 ]
      [ "$output" = "" ]
      ;;
    busybox)
      run_lab_c 'flock -n -E 42 "$1" echo ran' inline "$LOCK"
      [ "$status" -eq 1 ]
      [[ $output != *ran* ]]
      ;;
    bsd-lockf)
      run_lab_c 'lockf -E 42 "$1" echo ran' inline "$LOCK"
      [ "$status" -eq 64 ]
      [[ $output == *"illegal option"* ]]
      ;;
    *)
      false
      ;;
  esac
}

@test "a bounded wait gives up after the timeout: flock -w 1 exits 1, lockf -t 1 exits 75" {
  local timed='s=$(date +%s); "$@"; rc=$?; e=$(date +%s); echo "$rc $((e - s))"'
  case $(lock_impl) in
    util-linux)
      hold_lock 5
      run_lab_c "$timed" inline flock -w 1 "$LOCK" echo ran
      ;;
    busybox)
      run_lab_c 'flock -w 1 "$1" echo ran' inline "$LOCK"
      [ "$status" -eq 1 ]
      [[ $output != *ran* ]]
      return 0
      ;;
    bsd-lockf)
      hold_lock 5
      run_lab_c "$timed" inline lockf -s -k -t 1 "$LOCK" echo ran
      ;;
    *)
      false
      ;;
  esac
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2086,SC2248 # split "rc seconds" into two fields
  set -- $output
  case $(lock_impl) in
    util-linux) [ "$1" -eq 1 ] ;;
    bsd-lockf) [ "$1" -eq 75 ] ;;
    *) false ;;
  esac
  [ "$2" -ge 1 ]
  [ "$2" -le 4 ]
}

@test "flock(1) lets a command's background child keep the lock unless -o; lockf(1) always closes it" {
  local pidfile=$BATS_TEST_TMPDIR/bg.pid
  local probe='if flock -n "$1" true; then echo free; else echo busy; fi; read -r p <"$3"; kill "$p"'
  case $(lock_impl) in
    util-linux)
      run_lab_c 'flock "$1" "$2" "$3"; '"$probe" inline "$LOCK" "$LAB_DIR/bg_child.sh" "$pidfile"
      [ "$output" = "busy" ]
      run_lab_c 'flock -o "$1" "$2" "$3"; '"$probe" inline "$LOCK" "$LAB_DIR/bg_child.sh" "$pidfile"
      [ "$output" = "free" ]
      ;;
    busybox)
      run_lab_c 'flock "$1" "$2" "$3"; '"$probe" inline "$LOCK" "$LAB_DIR/bg_child.sh" "$pidfile"
      [ "$output" = "busy" ]
      rm -f "$pidfile"
      run_lab_c 'flock -o "$1" "$2" "$3"' inline "$LOCK" "$LAB_DIR/bg_child.sh" "$pidfile"
      [ "$status" -eq 1 ]
      [ ! -e "$pidfile" ]
      ;;
    bsd-lockf)
      run_lab_c 'lockf -k "$1" "$2" "$3"; if lockf -s -t 0 -k "$1" true; then echo free; else echo busy; fi; read -r p <"$3"; kill "$p"' \
        inline "$LOCK" "$LAB_DIR/bg_child.sh" "$pidfile"
      [ "$output" = "free" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "removing the lock file while a waiter is queued lets two processes hold the lock" {
  run_lab "$LAB_DIR/flock_demo.sh" rm-race "$LOCK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "B acquired" ]
  [ "${lines[1]}" = "C acquired" ]
}

@test "keeping the lock file makes the late opener wait its turn" {
  run_lab "$LAB_DIR/flock_demo.sh" keep-file "$LOCK"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "B acquired" ]
  [ "${lines[1]}" = "C blocked" ]
}

@test "lockf(1) deletes its lock file unless -k; flock(1) never deletes it" {
  local check='p=$1; shift; "$@"; if [ -e "$p" ]; then echo exists; else echo missing; fi'
  case $(lock_impl) in
    util-linux | busybox)
      run_lab_c "$check" inline "$LOCK" flock "$LOCK" true
      [ "$output" = "exists" ]
      ;;
    bsd-lockf)
      run_lab_c "$check" inline "$LOCK" lockf "$LOCK" true
      [ "$output" = "missing" ]
      run_lab_c "$check" inline "$LOCK" lockf -k "$LOCK" true
      [ "$output" = "exists" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "flock -c runs its string through \$SHELL, not /bin/sh" {
  case $(lock_impl) in
    util-linux | busybox)
      run_lab_c 'SHELL=$2 flock "$1" -c "echo hi"' inline "$LOCK" "$LAB_DIR/fake_shell.sh"
      [ "$status" -eq 0 ]
      [ "$output" = "fake shell got: -c echo hi" ]
      ;;
    bsd-lockf)
      run_lab_c 'command -v flock'
      [ "$status" -ne 0 ]
      [ "$output" = "" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "Linux /proc/locks lists the lock as FLOCK ADVISORY WRITE owned by the flock process" {
  if [ ! -e /proc/locks ]; then
    # shellcheck disable=SC2312 # a failed uname shows up as a mismatch
    [ "$(uname -s)" = Darwin ]
    return 0
  fi
  hold_lock 4
  local prog='$2 == "FLOCK" { print $2, $3, $4, $5 }'
  run_lab_c 'awk "$1" /proc/locks' inline "$prog"
  [ "$status" -eq 0 ]
  [[ $'\n'$output$'\n' == *$'\n'"FLOCK ADVISORY WRITE $HOLDER"$'\n'* ]]
}
