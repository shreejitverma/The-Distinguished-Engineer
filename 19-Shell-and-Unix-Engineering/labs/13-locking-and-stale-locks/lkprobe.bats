#!/usr/bin/env bats
# Tests for lock.sh and the locking failure windows, run under the shell under test (LAB_SHELL).

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/lkprobe.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
  export LK_SH="${LAB_SHELL:-sh}"
}

build_lkc() {
  export LKC="$BATS_TEST_TMPDIR/lkc"
  "${CC:-cc}" -O1 -Wall -Werror -o "$LKC" "$BATS_TEST_DIRNAME/lkc.c"
}

@test "1. without a lock two read-modify-write workers lose an update" {
  run_lab "$PROBE" counter nolock
  [ "$status" -eq 0 ]
  [ "$output" = "count=1" ]
}

@test "2. with lock.sh the second worker waits and no update is lost" {
  run_lab "$PROBE" counter lock
  [ "$status" -eq 0 ]
  [ "$output" = "count=2" ]
}

@test "3. set -C lets every contender open a symlink to /dev/null; ln -s refuses the name" {
  run_lab "$PROBE" noclobber-devnull
  [ "$status" -eq 0 ]
  [ "$output" = "devnull_first=ok devnull_second=ok regular_second=fail ln_on_devnull=fail" ]
}

@test "4. a failed set -C redirection on the special built-in : ends a POSIX shell" {
  family=$(lab_shell_family)
  if [ "$family" = bash ]; then
    # shellcheck disable=SC2016 # the expansion belongs to the shell under test
    run_lab_c 'case :${SHELLOPTS-}: in *:posix:*) echo posix ;; *) echo normal ;; esac'
    family="bash-$output"
  elif [ "$family" = yash ]; then
    # yash continues after the failed redirection unless its posix option is set
    run_lab_c 'if [ -o posix ]; then echo posix; else echo normal; fi'
    family="yash-$output"
  fi
  run_lab "$PROBE" noclobber-colon
  case $family in
    bash-normal | zsh)
      [ "$status" -eq 0 ]
      [ "$output" = "survived status=1" ]
      ;;
    yash-normal)
      [ "$status" -eq 0 ]
      [ "$output" = "survived status=2" ]
      ;;
    bash-posix | zsh-sh | dash | ksh93 | mksh | yash-posix | posh | busybox)
      [ "$status" -ne 0 ]
      [ "$output" = "" ]
      ;;
    *) false ;;
  esac
}

@test "5. a SIGKILLed holder leaves its lock behind and a contender breaks it" {
  run_lab "$PROBE" stale-kill
  [ "$status" -eq 0 ]
  [ "$output" = "after_kill=present contender=acquired" ]
}

@test "6. a live holder is never broken, a timed acquire gives up, a foreign release is refused" {
  run_lab "$PROBE" live
  [ "$status" -eq 0 ]
  [ "$output" = "try=1 acquire_1s=1 release_foreign=1 lock=present after_holder=acquired" ]
}

@test "7. kill -0 calls a reused PID alive; the start-time token does not" {
  run_lab "$PROBE" pid-reuse
  [ "$status" -eq 0 ]
  [ "$output" = "kill0=0 wrong_token=1 right_token=0 foreign_host=2" ]
}

@test "8. a naive stale-lock breaker removes a fresh lock and two processes hold it" {
  run_lab "$PROBE" break-race naive
  [ "$status" -eq 0 ]
  [ "$output" = "naive: holders=2" ]
}

@test "9. re-checking under a break lock leaves exactly one holder" {
  run_lab "$PROBE" break-race safe
  [ "$status" -eq 0 ]
  [ "$output" = "safe: holders=1" ]
}

@test "10. the kernel drops a flock lock when its holder is SIGKILLed" {
  build_lkc
  run_lab "$PROBE" kernel-kill flock
  [ "$status" -eq 0 ]
  [ "$output" = "while_held=busy after_kill=free" ]
}

@test "11. the kernel drops an fcntl lock when its holder is SIGKILLed" {
  build_lkc
  run_lab "$PROBE" kernel-kill fcntl
  [ "$status" -eq 0 ]
  [ "$output" = "while_held=busy after_kill=free" ]
}

@test "12. a forked child keeps a flock lock after its parent exits; an fcntl lock is not inherited" {
  build_lkc
  run_lab "$PROBE" kernel-inherit flock
  [ "$status" -eq 0 ]
  [ "$output" = "after_parent_exit=busy" ]
  run_lab "$PROBE" kernel-inherit fcntl
  [ "$status" -eq 0 ]
  [ "$output" = "after_parent_exit=free" ]
}

@test "13. closing any descriptor for the file drops an fcntl lock but not a flock lock" {
  build_lkc
  run_lab "$PROBE" kernel-close-trap fcntl
  [ "$status" -eq 0 ]
  [ "$output" = "before_close=busy after_close=free" ]
  run_lab "$PROBE" kernel-close-trap flock
  [ "$status" -eq 0 ]
  [ "$output" = "before_close=busy after_close=busy" ]
}

@test "14. removing a held flock lock file lets a second process lock the new inode" {
  build_lkc
  run_lab "$PROBE" kernel-unlink
  [ "$status" -eq 0 ]
  [ "$output" = "after_unlink=acquired" ]
}
