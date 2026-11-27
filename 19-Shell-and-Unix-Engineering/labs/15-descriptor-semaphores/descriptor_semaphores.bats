#!/usr/bin/env bats
# Tests for Lab 15: Descriptor Semaphores (a FIFO opened read-write is a counting semaphore).

setup() {
  load ../lib/lab
  SRC=$BATS_TEST_DIRNAME
  FIFOTOOL=$BATS_TEST_TMPDIR/fifotool
  "${CC:-cc}" -Wall -Wextra -Werror -O1 -o "$FIFOTOOL" "$SRC/fifotool.c"
  export FIFOTOOL
  mkdir "$BATS_TEST_TMPDIR/d"
}

@test "pool: three tokens bound six one-second jobs to a peak of exactly three and every token comes back" {
  run_lab "$SRC/pool.sh" 3 6 "$BATS_TEST_TMPDIR/log" "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'max=3 jobs=6\ntokens=3')" ]
}

@test "pool: one token makes a mutex, peak 1" {
  run_lab "$SRC/pool.sh" 1 3 "$BATS_TEST_TMPDIR/log" "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'max=1 jobs=3\ntokens=1')" ]
}

@test "pool control: without the semaphore the same six jobs all overlap, so the peak measurement can see concurrency" {
  run_lab "$SRC/pool.sh" unbounded 6 "$BATS_TEST_TMPDIR/log"
  [ "$status" -eq 0 ]
  [ "$output" = "max=6 jobs=6" ]
}

@test "read takes exactly one token from a FIFO holding five" {
  run_lab "$SRC/semprobe.sh" read1 "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "left=4" ]
}

@test "dd bs=1 count=1 takes exactly one token from a FIFO holding five" {
  run_lab "$SRC/semprobe.sh" dd1 "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "left=4" ]
}

@test "read waits for a newline, so it swallows every non-newline token queued before it" {
  run_lab "$SRC/semprobe.sh" plusread "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "got=++ left=0" ]
}

@test "acquire on an empty semaphore blocks, and a post wakes it" {
  run_lab "$SRC/semprobe.sh" block "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "blocked=yes woke=yes status=0" ]
}

@test "kill -9 on a worker that returns its own token leaks the token" {
  run_lab "$SRC/semprobe.sh" kill "$BATS_TEST_TMPDIR/d" A
  [ "$status" -eq 0 ]
  [ "$output" = "pattern=A left=2" ]
}

@test "kill -9 on a worker whose supervisor returns the token loses nothing" {
  run_lab "$SRC/semprobe.sh" kill "$BATS_TEST_TMPDIR/d" B
  [ "$status" -eq 0 ]
  [ "$output" = "pattern=B left=3" ]
}

@test "descriptor 3 reaches an external command except where the shell sets close-on-exec, and 3>&- always closes it" {
  run_lab "$SRC/semprobe.sh" inherit "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    ksh93 | mksh)
      # both mark descriptors above 2 opened by exec close-on-exec (mksh R59, ksh93u+ and ksh93u+m)
      [ "$output" = "plain=closed closed=closed" ]
      ;;
    bash | zsh | zsh-sh | yash | busybox | posh | dash)
      [ "$output" = "plain=open closed=closed" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "read on a pipe whose writer is gone returns status 1 at once instead of blocking" {
  run_lab "$SRC/semprobe.sh" eof "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "status=1" ]
}

@test "a FIFO takes at least one page of one-byte tokens before a write would block" {
  run_lab "$SRC/semprobe.sh" capacity "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  case $output in
    capacity=[0-9]*) ;;
    *) false ;;
  esac
  [ "${output#capacity=}" -ge 4096 ]
}

@test "a client takes and returns a token from a make-style jobserver FIFO with dd" {
  run_lab "$SRC/semprobe.sh" jobserver "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'token=+\nheld=1\nreturned=2')" ]
}
