#!/usr/bin/env bats
# Tests for fifo-ipc.sh and fifo-probe.c, the lab of the note "Named Pipes for IPC".
# Every script runs under the shell under test (LAB_SHELL); the C probe is compiled once per test.
# Each mode prints key=value lines; keys_of keeps only those, so a shell's stderr notices (a job
# status line, "Broken pipe") cannot change the comparison.

setup() {
  load ../lib/lab
  LAB_DIR="$BATS_TEST_DIRNAME"
  SCRIPT="$LAB_DIR/fifo-ipc.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
  export LAB_PROBE="$BATS_TEST_TMPDIR/fifo-probe"
}

teardown() {
  # Every mode must remove its scratch directory, even after it kills a blocked child.
  local leftover
  leftover=$(find "$BATS_TEST_TMPDIR" -name 'fifo-ipc.*')
  [ -z "$leftover" ]
}

# build_probe: compile the C probe with the compiler the environment names; a failure fails the test.
build_probe() {
  "${CC:-cc}" -Wall -Wextra -Werror -O2 -o "$LAB_PROBE" "$LAB_DIR/fifo-probe.c"
}

# keys_of: print the key=value lines of $output, in order.
keys_of() {
  printf '%s\n' "$output" | grep -E '^[A-Za-z0-9.-]+='
}

# expect_keys EXPECTED: the key=value lines of $output equal EXPECTED exactly.
expect_keys() {
  local got
  got=$(keys_of)
  if [ "$got" != "$1" ]; then
    printf 'expected:\n%s\ngot:\n%s\nfull output:\n%s\n' "$1" "$got" "$output" >&2
    return 1
  fi
}

# probe MODE ARGS...: run the compiled probe through the shell under test.
probe() {
  run_lab_c '"$@"' probe "$LAB_PROBE" "$@"
}

nl=$'\n'

@test "rendezvous: a writer blocks in open until a reader arrives" {
  run_lab "$SCRIPT" rendezvous
  [ "$status" -eq 0 ]
  expect_keys "writer-finished-without-reader=no${nl}writer-finished-after-reader=yes${nl}payload=hello"
}

@test "eof-cycle: EOF reaches the reader when the last writer closes and each session needs a reopen" {
  run_lab "$SCRIPT" eof-cycle
  [ "$status" -eq 0 ]
  expect_keys "first-reader-saw-eof=yes${nl}first-reader-data=a${nl}second-writer-finished-without-reader=no${nl}second-reader-data=b"
}

@test "hold-open: a read-write descriptor keeps the reader from seeing EOF between writers" {
  run_lab "$SCRIPT" hold-open
  [ "$status" -eq 0 ]
  expect_keys "reader-saw-eof-while-held=no${nl}data-while-held=a,b${nl}reader-saw-eof-after-release=yes"
}

@test "leaked-descriptor: a child holding the read-write descriptor delays EOF until it exits" {
  run_lab "$SCRIPT" leaked-descriptor
  [ "$status" -eq 0 ]
  expect_keys "reader-saw-eof-after-parent-closed=no${nl}data=a${nl}reader-saw-eof-after-leaker-exited=yes"
}

@test "competing-readers: two readers split one stream and never receive copies" {
  run_lab "$SCRIPT" competing-readers
  [ "$status" -eq 0 ]
  expect_keys "written=3000${nl}read-total=3000"
}

@test "many-writers: four writers of 100-byte lines produce 800 intact lines" {
  build_probe
  run_lab "$SCRIPT" many-writers
  [ "$status" -eq 0 ]
  expect_keys "lines=800${nl}bad=0${nl}A=200 B=200 C=200 D=200"
}

@test "rpc: one request FIFO and a private reply FIFO per client route every reply" {
  run_lab "$SCRIPT" rpc
  [ "$status" -eq 0 ]
  expect_keys "reply.c1=HELLO${nl}reply.c2=WORLD${nl}reply.c3=FIFO${nl}server-exited=yes"
}

@test "reopen-loss: reopening the FIFO per iteration drops data, one open does not" {
  run_lab "$SCRIPT" reopen-loss
  [ "$status" -eq 0 ]
  expect_keys "reopen-loop-finished=no${nl}reopen-loop-lines=a${nl}single-open-lines=a,b,c"
}

@test "open-order: opposite open order deadlocks, read-write opens do not" {
  run_lab "$SCRIPT" open-order
  [ "$status" -eq 0 ]
  expect_keys "plain-a-finished=no${nl}plain-b-finished=no${nl}rdwr-c-finished=yes${nl}rdwr-e-finished=yes"
}

@test "reader-dies: the writer is killed by SIGPIPE, with the status each shell reports for it" {
  run_lab "$SCRIPT" reader-dies
  [ "$status" -eq 0 ]
  local code
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | busybox | posh | mksh) code=141 ;;
    ksh93) code=269 ;;
    yash) code=397 ;;
    *) false ;;
  esac
  expect_keys "writer-status=${code}${nl}writer-survived-second-write=no${nl}reader-got=one"
}

@test "mkfifo-mode: mkfifo honors the umask unless -m is given" {
  run_lab "$SCRIPT" mkfifo-mode
  [ "$status" -eq 0 ]
  expect_keys "m1=fifo prw-------${nl}m2=fifo prw-r--r--${nl}m3=fifo prw-------${nl}m4=fifo prw-rw-rw-"
}

@test "an unknown mode prints the usage and exits 2" {
  run_lab "$SCRIPT" no-such-mode
  [ "$status" -eq 2 ]
  case $output in
    *"usage: fifo-ipc.sh rendezvous|"*) ;;
    *) false ;;
  esac
}

@test "probe open-flags: the open(2) outcomes for each access mode and O_NONBLOCK" {
  build_probe
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c 'mkfifo "$1"' mk "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  probe open-flags "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  expect_keys "wronly-nonblock-no-reader=ENXIO${nl}rdonly-nonblock-no-writer=ok${nl}read-no-writer=0${nl}wronly-nonblock-with-reader=ok${nl}read-empty-with-writer=EAGAIN${nl}loopback=x${nl}read-after-last-writer-closed=0${nl}rdwr-blocking-no-peer=ok"
}

@test "probe poll-hup: poll reports data, and HUP after the last writer closes only where the kernel does" {
  build_probe
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c 'mkfifo "$1"' mk "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  probe poll-hup "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  local hup
  case $(uname -s) in
    Linux) hup=HUP ;;
    Darwin) hup=none ;;
    *) false ;;
  esac
  expect_keys "poll-reader-before-any-writer=none${nl}poll-reader-writer-open-empty=none${nl}poll-reader-writer-open-data=IN${nl}poll-reader-writer-closed=${hup}${nl}select-reader-writer-closed=readable"
}

@test "probe capacity: a FIFO fills at the kernel's buffer size before EAGAIN" {
  build_probe
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c 'mkfifo "$1"' mk "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  probe capacity "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  local keys bytes size
  keys=$(keys_of)
  bytes=${keys#*bytes-before-eagain=}
  bytes=${bytes%%$'\n'*}
  size=${keys#*f-getpipe-sz=}
  size=${size%%$'\n'*}
  case $(uname -s) in
    Linux) [ "$bytes" -eq "$size" ] && [ "$bytes" -ge 4096 ] ;;
    Darwin) [ "$bytes" = 8192 ] && [ "$size" = n/a ] ;;
    *) false ;;
  esac
}
