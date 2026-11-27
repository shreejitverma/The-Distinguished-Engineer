#!/usr/bin/env bats
# Tests for Graceful Shutdown.
# A shell's $? cannot tell "exited 143" from "killed by SIGTERM", and a service cannot be signalled
# from inside the test shell without races, so every service starts under supervise (a small C
# program that plays the supervisor: signal, grace period, SIGKILL, then report the kernel's view).
# The shell under test runs `exec "$@"` through run_lab_c to become supervise, and supervise starts
# the shell under test (LAB_SHELL) on the script, so the matrix still swaps the shell.

setup() {
  load ../lib/lab
  SERVICE="$BATS_TEST_DIRNAME/service.sh"
  PROBE="$BATS_TEST_DIRNAME/wait-probe.sh"
  SUP="$BATS_TEST_TMPDIR/supervise"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -o "$SUP" "$BATS_TEST_DIRNAME/supervise.c"
  W="$BATS_TEST_TMPDIR/work"
  mkdir "$W"
}

# observe [SUPERVISE-OPTION...] -- SCRIPT [ARG...]: run SCRIPT under supervise, in the shell under test.
observe() {
  local opts=()
  while [ "$1" != -- ]; do
    opts+=("$1")
    shift
  done
  shift
  lab_shell_words
  # shellcheck disable=SC2016 # "$@" belongs to the shell under test
  run_lab_c 'exec "$@"' sh "$SUP" -r "$W/ready" "${opts[@]}" -- "${LAB_SHELL_WORDS[@]}" "$@"
}

# expect_line TEXT: supervise printed exactly the line TEXT.
expect_line() {
  printf '%s\n' "$output" | grep -qx -- "$1"
}

# field NAME: set FIELD to the value of supervise's "NAME value" line; fail when there is none.
field() {
  local line
  FIELD=
  for line in "${lines[@]}"; do
    case $line in
      "$1 "*)
        FIELD=${line#"$1 "}
        return 0
        ;;
      *) ;;
    esac
  done
  return 1
}

# expect_field NAME VALUE / expect_field_ge NAME N / expect_field_lt NAME N
expect_field() {
  field "$1" || return 1
  [ "$FIELD" = "$2" ]
}
expect_field_ge() {
  field "$1" || return 1
  [ "$FIELD" -ge "$2" ]
}
expect_field_lt() {
  field "$1" || return 1
  [ "$FIELD" -lt "$2" ]
}

# log_has TEXT / log_lacks TEXT: the service log has, or lacks, exactly the line TEXT.
log_has() {
  grep -qx -- "$1" "$W/log"
}
log_lacks() {
  ! grep -qx -- "$1" "$W/log"
}

# expect_log_count TEXT N: exactly N log lines equal TEXT.
expect_log_count() {
  local n
  n=$(grep -cx -- "$1" "$W/log") || true
  [ "$n" -eq "$2" ]
}

# log_line TEXT: set LINE to the line number of the first log line equal to TEXT.
log_line() {
  LINE=$(grep -nx -- "$1" "$W/log") || return 1
  LINE=${LINE%%$'\n'*}
  LINE=${LINE%%:*}
}

# expect_log_before A B: the log has line A, and line B after it.
expect_log_before() {
  local first
  log_line "$1" || return 1
  first=$LINE
  log_line "$2" || return 1
  [ "$first" -lt "$LINE" ]
}

@test "control: without a signal the service runs every job and exits 0" {
  run_lab "$SERVICE" drain "$W" 1 2
  [ "$status" -eq 0 ]
  log_has "start 1"
  log_has "done 1 ok"
  log_has "start 2"
  log_has "done 2 ok"
  log_has "exit complete 2"
  log_lacks "term-received"
}

@test "drain: SIGTERM lets the in-flight job finish, takes no new job, and exits 0" {
  observe -t TERM -- "$SERVICE" drain "$W" 4
  [ "$status" -eq 0 ]
  expect_line "exited 0"
  expect_field escalated no
  expect_field stragglers no
  expect_field sent 1
  log_has "term-received"
  log_has "done 1 ok"
  log_has "exit drained 1"
  log_lacks "start 2"
  # The job finished after the signal arrived: the stop waited for it.
  expect_log_before "term-received" "done 1 ok"
  # The signal came 1 s into a 4 s job, so about 3 s of work remained; 1 s margin on the bound.
  expect_field_ge elapsed_ms 1000
}

@test "drain: SIGINT is handled like SIGTERM" {
  observe -t INT -- "$SERVICE" drain "$W" 3
  [ "$status" -eq 0 ]
  expect_line "exited 0"
  log_has "term-received"
  log_has "done 1 ok"
  log_has "exit drained 1"
  log_lacks "start 2"
}

@test "drain: a second signal kills the in-flight job and exits 3" {
  observe -t TERM -n 2 -i 1 -- "$SERVICE" drain "$W" 6
  [ "$status" -eq 0 ]
  expect_line "exited 3"
  expect_field sent 2
  expect_field escalated no
  expect_field stragglers no
  log_has "term-received"
  log_has "force-requested"
  log_has "done 1 killed"
  log_has "exit forced 1"
  log_lacks "done 1 ok"
  # About 5 s of work remained after the first signal; the force came 1 s later.
  expect_field_lt elapsed_ms 4000
}

@test "abort: the trap kills the in-flight job and the exit status is a plain 143, not death by TERM" {
  observe -t TERM -- "$SERVICE" abort "$W" 4
  [ "$status" -eq 0 ]
  expect_line "exited 143"
  expect_field escalated no
  expect_field stragglers no
  log_has "term-received"
  log_has "exit aborted 1"
  log_lacks "done 1 ok"
  log_lacks "done 1 killed"
  expect_field_lt elapsed_ms 3000
}

@test "foreground job: POSIX defers the trap until the job ends, and two signals collapse into one trap except in zsh" {
  # The job is a foreground /bin/sleep 4. TERM arrives 1 s in and again 1 s later; the shell
  # takes no trap while the foreground command runs (POSIX.1-2024 XCU 2.12). Most shells keep one
  # pending flag per trapped signal (bash set_trap_state and run_pending_traps, dash onsig and
  # gotsig), so one trap runs after the job and the second request is lost.
  observe -t TERM -n 2 -i 1 -- "$SERVICE" foreground "$W" 4
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | ksh93)
      expect_line "exited 0"
      expect_log_count "term-received" 1
      log_lacks "force-requested"
      log_has "done 1 ok"
      log_has "exit drained 1"
      log_lacks "start 2"
      ;;
    zsh | zsh-sh)
      # zsh queues every arrival (zhandler, signal_queue) and runs both traps after the job, so the
      # second signal counts as a force request even though the job already finished.
      expect_line "exited 3"
      expect_log_count "term-received" 1
      expect_log_count "force-requested" 1
      log_has "done 1 ok"
      log_has "exit forced 1"
      log_lacks "start 2"
      ;;
    *) skip "no measured result for shell family $(lab_shell_family)" ;;
  esac
  # The stop waited for the job: it had about 3 s left when the first signal arrived.
  expect_field_ge elapsed_ms 1000
}

@test "stubborn: a service that ignores SIGTERM runs out the grace period and dies by SIGKILL" {
  observe -t TERM -w 2 -- "$SERVICE" stubborn "$W" 100
  [ "$status" -eq 0 ]
  expect_line "signaled KILL"
  expect_field escalated yes
  expect_field stragglers no
  log_has "term-ignored"
  log_lacks "exit drained 1"
  # SIGKILL went out 2 s after the first signal, not before.
  expect_field_ge elapsed_ms 2000
}

@test "grace shorter than the drain: SIGKILL lands mid-job and the job is lost" {
  observe -t TERM -w 1 -- "$SERVICE" drain "$W" 6
  [ "$status" -eq 0 ]
  expect_line "signaled KILL"
  expect_field escalated yes
  log_has "term-received"
  log_lacks "done 1 ok"
  log_lacks "exit drained 1"
  # The drain needed about 5 s more; the grace was 1 s.
  expect_field_lt elapsed_ms 4000
}

@test "SIGTERM to the whole group also kills the in-flight job (systemd KillMode=control-group)" {
  observe -G -t TERM -- "$SERVICE" drain "$W" 4
  [ "$status" -eq 0 ]
  expect_line "exited 0"
  expect_field escalated no
  log_has "term-received"
  log_has "done 1 killed"
  log_lacks "done 1 ok"
  log_has "exit drained 1"
  # Without the group signal the same drain takes about 3 s (the drain test above); here it does not wait.
  expect_field_lt elapsed_ms 2000
}

@test "wait: a trapped signal interrupts wait while the child still runs, and a second wait collects it" {
  observe -t TERM -- "$PROBE" interrupted "$W"
  [ "$status" -eq 0 ]
  expect_line "exited 0"
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash)
      # POSIX.1-2024 XCU 2.12: wait returns immediately with a status above 128.
      expect_line "first_wait status=gt128 child_alive=yes trap_ran=1"
      ;;
    ksh93)
      # An interrupted wait returns 1 in ksh93 (job_wait sets exitval = 1), not 128 + N.
      expect_line "first_wait status=other:1 child_alive=yes trap_ran=1"
      ;;
    *) skip "no measured result for shell family $(lab_shell_family)" ;;
  esac
  expect_line "second_wait status=zero"
}

@test "wait: a child killed by TERM gives the same status above 128, and only kill -0 says it is gone" {
  run_lab "$PROBE" killed "$W"
  [ "$status" -eq 0 ]
  expect_line "first_wait status=gt128 child_alive=no trap_ran=0"
}

@test "service.sh and wait-probe.sh reject a bad mode" {
  run_lab "$SERVICE" bogus "$W" 1
  [ "$status" -eq 2 ]
  run_lab "$PROBE" bogus "$W"
  [ "$status" -eq 2 ]
}
