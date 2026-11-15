#!/usr/bin/env bats
# Kernel lock semantics: flock(2), process-owned fcntl(2) locks, and OFD fcntl(2) locks.
# lockprobe.c does the system calls; each test asserts the exact facts it prints.
# Linux and macOS (XNU) differ in three places, so those tests branch on the kernel.

setup() {
  load ../lib/lab
  PROBE=$BATS_TEST_TMPDIR/lockprobe
  "${CC:-cc}" -std=c99 -Wall -Wextra -Werror -O2 -o "$PROBE" "$BATS_TEST_DIRNAME/lockprobe.c"
  F=$BATS_TEST_TMPDIR/lockfile
  : >"$F"
}

@test "flock and OFD locks belong to the open, fcntl locks to the process" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" owner "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "flock, second open in the same process: busy" ]
  [ "${lines[1]}" = "fcntl, second open in the same process: granted" ]
  [ "${lines[2]}" = "ofd, second open in the same process: busy" ]
  [ "${lines[3]}" = "ofd held, fcntl in the same process: busy" ]
}

@test "closing any descriptor for the file drops a process-owned fcntl lock" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" close "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "flock after closing an unrelated descriptor: busy" ]
  [ "${lines[1]}" = "fcntl after closing an unrelated descriptor: free" ]
  [ "${lines[2]}" = "ofd after closing an unrelated descriptor: busy" ]
}

@test "a forked child shares flock and OFD locks but never inherits fcntl locks" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" fork "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "flock after the parent closes, child still open: busy" ]
  [ "${lines[1]}" = "fcntl after the parent closes, child still open: free" ]
  [ "${lines[2]}" = "ofd after the parent closes, child still open: busy" ]
}

@test "an exclusive flock needs no write access; an fcntl write lock does" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" rdonly "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "flock exclusive lock on an O_RDONLY descriptor: ok" ]
  [ "${lines[1]}" = "fcntl exclusive lock on an O_RDONLY descriptor: EBADF" ]
  [ "${lines[2]}" = "ofd exclusive lock on an O_RDONLY descriptor: EBADF" ]
}

@test "flock and fcntl locks are independent on Linux and conflict on macOS" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" mix "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Linux)
      [ "${lines[0]}" = "flock held, fcntl tester: free" ]
      [ "${lines[1]}" = "fcntl held, flock tester: free" ]
      ;;
    Darwin)
      [ "${lines[0]}" = "flock held, fcntl tester: busy" ]
      [ "${lines[1]}" = "fcntl held, flock tester: busy" ]
      ;;
    *) false ;;
  esac
  # POSIX.1-2024 requires OFD-owned and process-owned locks to conflict everywhere.
  [ "${lines[2]}" = "ofd held, fcntl tester: busy" ]
}

@test "F_GETLK names a process only for process-owned locks" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" getlk "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Linux) [ "${lines[0]}" = "F_GETLK on a flock lock: no conflict reported" ] ;;
    Darwin) [ "${lines[0]}" = "F_GETLK on a flock lock: l_pid -1" ] ;;
    *) false ;;
  esac
  [ "${lines[1]}" = "F_GETLK on a fcntl lock: l_pid is the holder" ]
  [ "${lines[2]}" = "F_GETLK on a ofd lock: l_pid -1" ]
}

@test "a failed non-blocking flock upgrade drops the shared lock on Linux only" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" upgrade "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "upgrade while another process shares: busy" ]
  case $(uname -s) in
    Linux) [ "${lines[1]}" = "shared lock after the failed upgrade: lost" ] ;;  # fs/locks.c flock_lock_inode()
    Darwin) [ "${lines[1]}" = "shared lock after the failed upgrade: kept" ] ;; # XNU kern_lockf.c lf_setlock()
    *) false ;;
  esac
}

@test "an AB-BA cycle of fcntl locks fails with EDEADLK" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" deadlock fcntl "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "fcntl AB-BA cycle: EDEADLK" ]
}

@test "an AB-BA cycle of OFD locks is not detected" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" deadlock ofd "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "ofd AB-BA cycle: undetected, B waited until its 3 s alarm (EINTR)" ]
}

@test "an AB-BA cycle of flock locks on two files is not detected" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" deadlock flock "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "flock AB-BA cycle: undetected, B waited until its 3 s alarm (EINTR)" ]
}

@test "bench times every kind of uncontended lock and unlock" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '"$1" bench 20000 "$2"' x "$PROBE" "$F"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 3 ]
  for i in 0 1 2; do
    kind=${lines[$i]%%:*}
    ns=${lines[$i]#*: }
    ns=${ns%% *}
    case $i in
      0) [ "$kind" = flock ] ;;
      1) [ "$kind" = fcntl ] ;;
      2) [ "$kind" = ofd ] ;;
      *) false ;;
    esac
    [ "${lines[$i]}" = "$kind: $ns ns per lock+unlock pair (n=20000)" ]
    # A real system call costs more than 10 ns and far less than 1 ms.
    [ "$ns" -gt 10 ]
    [ "$ns" -lt 1000000 ]
  done
}
