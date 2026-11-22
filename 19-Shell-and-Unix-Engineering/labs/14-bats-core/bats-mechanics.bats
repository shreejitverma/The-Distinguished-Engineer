#!/usr/bin/env bats
# bats-core observed from outside: each test writes a small fixture, runs the real `bats` on it in a
# scrubbed environment, and asserts on the TAP stream. bats itself is bash-only, so these tests call
# `bats` directly; the shell matrix does not change how it runs.

setup() {
  load ../lib/lab
  bats_require_minimum_version 1.10.0
  FIX="$BATS_TEST_DIRNAME/fixtures"
  WORK="$BATS_TEST_TMPDIR/work"
  MARK="$BATS_TEST_TMPDIR/mark"
  RELEASE="$BATS_TEST_TMPDIR/release"
  PIDFILE="$BATS_TEST_TMPDIR/pid"
  # bats puts its own libexec directory first on PATH inside a test, where `bats` names the internal
  # script that needs bin/bats to have run first; drop that entry to reach the real front end.
  CLEAN_PATH=":$PATH:"
  CLEAN_PATH=${CLEAN_PATH//":$BATS_LIBEXEC:"/:}
  CLEAN_PATH=${CLEAN_PATH#:}
  CLEAN_PATH=${CLEAN_PATH%:}
  mkdir "$WORK"
  cd "$WORK" || return 1
}

teardown() {
  # Never leave a fixture's background job behind: release it, then reap or kill the sleeper.
  : >"$RELEASE"
  if [ -s "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE")
    case $(ps -o comm= -p "$pid" 2>/dev/null) in
      *sleep) kill "$pid" ;;
      *) ;;
    esac
  fi
  if [ -e "$MARK.expected" ]; then
    lab_poll 20 test -e "$MARK" || true
  fi
}

# version_ge A B: true when the dotted version A is at least B.
version_ge() {
  local a b i
  IFS=. read -r -a a <<<"$1"
  IFS=. read -r -a b <<<"$2"
  for i in 0 1 2; do
    if ((a[i] > b[i])); then return 0; fi
    if ((a[i] < b[i])); then return 1; fi
  done
  return 0
}

# stage NAME: copy fixtures/NAME.fixture to NAME.bats in the work directory.
stage() {
  cp "$FIX/$1.fixture" "$WORK/$1.bats"
}

# inner VAR=value... bats ARGS...: run the bats on PATH in a scrubbed environment confined to WORK.
inner() {
  run env -i PATH="$CLEAN_PATH" HOME="$WORK" TMPDIR="$WORK" "$@"
}

# want_line TEXT: the whole of one output line equals TEXT; print the output when it does not.
want_line() {
  if ! printf '%s\n' "$output" | grep -qxF -- "$1"; then
    printf 'missing line: %s\n--- output ---\n%s\n' "$1" "$output" >&2
    return 1
  fi
}

# no_line TEXT: no output line equals TEXT.
no_line() {
  if printf '%s\n' "$output" | grep -qxF -- "$1"; then
    printf 'unexpected line: %s\n--- output ---\n%s\n' "$1" "$output" >&2
    return 1
  fi
}

@test "free code runs once per evaluation of the file, not once per file" {
  stage free-code
  inner COUNTER="$WORK/counter" bats --tap free-code.bats
  [ "$status" -eq 0 ]
  want_line "1..3"
  count=$(wc -l <"$WORK/counter")
  count=${count// /}
  if version_ge "$BATS_VERSION" 1.14.0; then
    # bats-gather-tests, bats-exec-file (setup_file) and each of the three bats-exec-test processes
    [ "$count" -eq 5 ]
  else
    # an older bats may skip one evaluation; either way the file is evaluated more than once per test
    [ "$count" -ge 4 ]
    [ "$count" -le 5 ]
  fi
}

@test "a leading bang in the middle of a test does not fail it, run bang does" {
  stage gotchas
  inner bats --tap gotchas.bats
  [ "$status" -eq 1 ]
  want_line "1..6"
  want_line "ok 1 bang mid-test is ignored"
  want_line "not ok 5 run bang fails when the command succeeds"
  want_line "ok 6 run bang passes when the command fails"
}

@test "a failing single bracket mid-test fails the test on every bash" {
  stage gotchas
  inner bats --tap gotchas.bats
  want_line "not ok 2 single bracket mid-test fails"
}

@test "a failing double bracket mid-test fails the test only where bash honours set -e for it" {
  stage gotchas
  # feature detection on the bash bats will use: bash 3.2 ignores errexit for [[ ]] and (( )) mid-function
  run bash -c 'set -eET; f() { [[ 1 = 2 ]]; echo reached; }; f'
  dbl=$output
  run bash -c 'set -eET; f() { ((1 == 2)); echo reached; }; f'
  ari=$output
  inner bats --tap gotchas.bats
  if [ "$dbl" = reached ]; then
    want_line "ok 3 double bracket mid-test"
  else
    want_line "not ok 3 double bracket mid-test"
  fi
  if [ "$ari" = reached ]; then
    want_line "ok 4 arithmetic mid-test"
  else
    want_line "not ok 4 arithmetic mid-test"
  fi
}

@test "a background job holds the bats output pipe open until it exits" {
  stage fd-leak
  : >"$MARK.expected"
  inner MARK="$MARK" bats --tap fd-leak.bats
  [ "$status" -eq 0 ]
  want_line "ok 1 background job inherits the TAP pipe"
  # bats could only return after the job closed its descriptors, which is after it created MARK
  [ -e "$MARK" ]
}

@test "closing every descriptor above 2 releases bats while the job keeps running" {
  stage fd-close
  : >"$MARK.expected"
  inner MARK="$MARK" RELEASE="$RELEASE" bats --tap fd-close.bats
  [ "$status" -eq 0 ]
  want_line "ok 1 background job with every extra descriptor closed"
  # the job is blocked on RELEASE, so MARK cannot exist yet: bats returned without waiting for it
  [ ! -e "$MARK" ]
  : >"$RELEASE"
  lab_poll 20 test -e "$MARK"
}

@test "BATS_TEST_TIMEOUT kills the test and its children" {
  stage timeout
  inner PIDFILE="$PIDFILE" BATS_TEST_TIMEOUT=2 bats --tap timeout.bats
  [ "$status" -eq 1 ]
  if version_ge "$BATS_VERSION" 1.14.0; then
    want_line "not ok 1 sleeps past the timeout # timeout after 2s"
  else
    printf '%s\n' "$output" | grep -qF "not ok 1 sleeps past the timeout"
  fi
  [ -s "$PIDFILE" ]
  sleeper=$(cat "$PIDFILE")
  gone() { ! kill -0 "$sleeper" 2>/dev/null; }
  lab_poll 10 gone
}

@test "hooks run in the order suite, file, test and unwind in reverse" {
  mkdir "$WORK/hooks"
  cp "$FIX/hooks/setup_suite.bash" "$WORK/hooks/setup_suite.bash"
  cp "$FIX/hooks/hooks.fixture" "$WORK/hooks/hooks.bats"
  inner TRACE="$WORK/trace" bats --tap hooks/hooks.bats
  [ "$status" -eq 0 ]
  want_line "ok 1 t1"
  want_line "ok 2 t2"
  order=$(paste -sd' ' "$WORK/trace")
  [ "$order" = "suite-setup file-setup setup t1 teardown setup t2 teardown file-teardown suite-teardown" ]
}

@test "a failing command that is not last in teardown does not fail the test" {
  stage teardown-lenient
  inner bats --tap teardown-lenient.bats
  [ "$status" -eq 0 ]
  want_line "after-false"
  want_line "ok 1 failing command that is not last in teardown"
}

@test "return 1 in teardown fails the test and stops teardown" {
  stage teardown-return
  inner bats --tap teardown-return.bats
  [ "$status" -eq 1 ]
  want_line "not ok 1 return 1 in teardown"
  no_line "after-return"
}

@test "test names are encoded into function names with hex escapes" {
  stage names
  inner bats --tap --filter 'a/b' names.bats
  [ "$status" -eq 0 ]
  want_line "# name=test_a-2fb_c-5fd-2de"
  want_line "ok 1 a/b c_d-e"
}

@test "variables set under run are lost, while the same call without run keeps them" {
  stage names
  inner bats --tap --filter 'variable set under run' names.bats
  [ "$status" -eq 0 ]
  want_line "# after-run=0"
  want_line "# direct=1"
}

@test "a pipe after run consumes nothing because run captured the output" {
  stage names
  inner bats --tap --filter 'pipe after run' names.bats
  [ "$status" -eq 0 ]
  want_line "# consumer-bytes=0"
}

@test "bats_pipe reports the last failing stage unless a position is given" {
  stage names
  inner bats --tap --filter 'bats_pipe' names.bats
  [ "$status" -eq 0 ]
  want_line "# default status=3 output=hi"
  want_line "# first status=3"
  want_line "# last status=0"
}

@test "run of a missing command passes but bats warns with BW01" {
  stage names
  inner bats --tap --filter 'missing command' names.bats
  [ "$status" -eq 0 ]
  want_line "# status=127"
  printf '%s\n' "$output" | grep -q '^BW01: '
}

@test "skip ends the test and reports the reason in the TAP line" {
  stage names
  inner bats --tap --filter 'skip stops' names.bats
  [ "$status" -eq 0 ]
  want_line "ok 1 skip stops the test # skip because"
  no_line "# unreachable"
}

@test "--count prints the number of tests without running them" {
  stage names
  inner bats --count names.bats
  [ "$status" -eq 0 ]
  [ "$output" = 6 ]
}

@test "--filter-tags selects by tag and an empty list selects the untagged tests" {
  stage tags
  inner bats --tap --filter-tags fast tags.bats
  want_line "1..1"
  want_line "ok 1 one"
  inner bats --tap --filter-tags '!slow' tags.bats
  want_line "1..2"
  want_line "ok 2 three"
  inner bats --tap --filter-tags '' tags.bats
  want_line "1..1"
  want_line "ok 1 three"
}

@test "a run containing a bats:focus test exits 1 unless BATS_NO_FAIL_FOCUS_RUN is set" {
  stage focus
  inner bats --tap focus.bats
  [ "$status" -eq 1 ]
  want_line "1..1"
  want_line "ok 1 focused"
  inner BATS_NO_FAIL_FOCUS_RUN=1 bats --tap focus.bats
  [ "$status" -eq 0 ]
}

@test "a directory with no tests fails unless the suite is allowed to be empty" {
  mkdir "$WORK/empty"
  inner bats --tap empty
  if version_ge "$BATS_VERSION" 1.14.0; then
    [ "$status" -eq 1 ]
    printf '%s\n' "$output" | grep -qF "Found no tests"
    inner bats --tap --allow-empty-suite empty
    [ "$status" -eq 0 ]
    want_line "1..0"
  else
    [ "$status" -eq 0 ]
  fi
}

@test "--errexit makes run stop a function at its first failing command" {
  stage errexit
  inner bats --tap errexit.bats
  [ "$status" -eq 0 ]
  want_line "# status=0 output=after"
  inner bats --tap --errexit errexit.bats
  if version_ge "$BATS_VERSION" 1.14.0; then
    [ "$status" -eq 0 ]
    want_line "# status=1 output="
  else
    [ "$status" -eq 1 ]
    printf '%s\n' "$output" | grep -qF "Bad command line option"
  fi
}
