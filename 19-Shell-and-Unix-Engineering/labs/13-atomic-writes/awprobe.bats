#!/usr/bin/env bats
# Atomic writes: a plain "> file" truncates the destination first and exposes every
# intermediate state, while atomic_write (atomic_write.sh) stages the content in a
# temporary file in the same directory and renames it over the destination.
# The shell under test runs awprobe.sh; only the ulimit -f unit differs by shell.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/awprobe.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

@test "a plain redirection truncates before the command runs: sort f > f leaves 0 bytes" {
  run_lab "$PROBE" truncate-first
  [ "$status" -eq 0 ]
  [ "$output" = "naive_bytes=0 atomic=a,b," ]
}

@test "a reader during the write sees partial content after >, the old file with atomic_write" {
  run_lab "$PROBE" reader-midwrite
  [ "$status" -eq 0 ]
  [ "$output" = "naive_seen=new-1 atomic_seen=OLD glob=dest hidden=1 final=new-1,new-2," ]
}

@test "a failing producer leaves a partial file after >, the old file and no temp with atomic_write" {
  run_lab "$PROBE" producer-fails
  [ "$status" -eq 0 ]
  [ "$output" = "naive_rc=3 naive=partial atomic_rc=3 atomic=OLD leftovers=none" ]
}

@test "a write that hits RLIMIT_FSIZE (EFBIG) truncates the file after >, not with atomic_write" {
  local unit family version
  family=$(lab_shell_family)
  version=$(lab_shell_version)
  # ulimit -f counts 512-byte blocks (POSIX); bash counts 1024 outside POSIX mode,
  # and bash 3.2 counts 1024 even in POSIX mode (macOS /bin/sh measured).
  case $family in
    bash)
      if run_lab_c '[ -o posix ]' && [ "$status" -eq 0 ] && [ "${version%%.*}" -ge 4 ]; then
        unit=512
      else
        unit=1024
      fi
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) unit=512 ;;
    *) false ;;
  esac
  run_lab "$PROBE" file-size-limit
  [ "$status" -eq 0 ]
  [ "$output" = "payload=18890 naive_rc=1 naive_bytes=$unit atomic_rc=1 atomic=OLD leftovers=none" ]
}

@test "the replacement carries the temporary file's mode unless atomic_write sets it" {
  run_lab "$PROBE" modes
  [ "$status" -eq 0 ]
  [ "$output" = "kept=-rw-r----- fresh=-rw-rw-r-- explicit=-rw------- mktemp_mv=-rw-------" ]
}

@test "the replacement is a new inode: an open descriptor and a hard link keep the old content" {
  run_lab "$PROBE" links
  [ "$status" -eq 0 ]
  [ "$output" = "name=NEW fd=OLD hardlink=OLD" ]
}

@test "atomic_write refuses a symlink; a bare temp-and-mv replaces the link, not its target" {
  run_lab "$PROBE" symlink
  [ "$status" -eq 0 ]
  [ "$output" = "refuse_rc=2 link=yes target=OLD raw_link=no raw_name=NEW raw_target=OLD" ]
}

@test "a temp name derived from a 250-byte name overflows NAME_MAX; the fixed .aw. template does not" {
  run_lab "$PROBE" name-max
  [ "$status" -eq 0 ]
  [ "$output" = "name_bytes=250 derived=fail aw_rc=0 content=NEW" ]
}

@test "an unwritable directory blocks atomic_write but not an in-place truncation" {
  # Root bypasses directory permissions, so this test means something only for a user.
  local uid
  uid=$(id -u)
  [ "$uid" -ne 0 ]
  run_lab "$PROBE" readonly-dir
  [ "$status" -eq 0 ]
  [ "$output" = "atomic_rc=2 after_atomic=OLD naive_rc=0 after_naive=NAIVE" ]
}

@test "SIGKILL during the producer leaves the old file and one hidden temp file" {
  local work content
  run_lab "$PROBE" sigkill
  [ "$status" -eq 137 ]
  [ "${output%%=*}" = work ]
  work=${output#work=}
  content=$(cat "$work/dest")
  [ "$content" = OLD ]
  set -- "$work"/.aw.*
  [ "$#" -eq 1 ]
  content=$(cat "$1")
  [ "$content" = partial ]
}

@test "atomic_create publishes once: link(2) refuses the second writer" {
  run_lab "$PROBE" create-once
  [ "$status" -eq 0 ]
  [ "$output" = "first_rc=0 second_rc=1 dest=FIRST leftovers=none" ]
}

@test "AW_SYNC=1 flushes through dd conv=fsync and sync DIR and still replaces the file" {
  run_lab "$PROBE" durable
  [ "$status" -eq 0 ]
  [ "$output" = "rc=0 dest=NEW leftovers=none flush_rc=0 other=keep" ]
}
