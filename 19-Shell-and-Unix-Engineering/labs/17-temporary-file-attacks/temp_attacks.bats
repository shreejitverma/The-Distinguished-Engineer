#!/usr/bin/env bats
# Tests for temporary file attack vulnerabilities and secure remediation patterns.

setup() {
  load ../lib/lab
  VULN="$BATS_TEST_DIRNAME/vulnerable_temp.sh"
  SECURE="$BATS_TEST_DIRNAME/secure_tempdir.sh"
  NOCLOBBER="$BATS_TEST_DIRNAME/noclobber_guard.sh"
}

@test "vulnerable_temp.sh follows pre-created symlink and overwrites target" {
  target="$BATS_TEST_TMPDIR/target_secret.txt"
  printf '%s\n' "ORIGINAL_TARGET_SECRET" >"$target"

  symlink="$BATS_TEST_TMPDIR/victim_job.tmp"
  ln -s "$target" "$symlink"

  run_lab "$VULN" "$BATS_TEST_TMPDIR" "victim_job"
  [ "$status" -eq 0 ]
  [ "$output" = "WROTE: $symlink" ]

  # Target file was clobbered via the symlink
  target_content=$(cat "$target")
  [ "$target_content" = "CRITICAL_PAYLOAD_DATA" ]
}

@test "noclobber_guard.sh blocks redirection when target is pre-existing symlink" {
  target="$BATS_TEST_TMPDIR/protected_secret.txt"
  printf '%s\n' "ORIGINAL_SECRET" >"$target"

  symlink="$BATS_TEST_TMPDIR/attack_symlink.tmp"
  ln -s "$target" "$symlink"

  run_lab "$NOCLOBBER" "$symlink" "ATTACK_PAYLOAD"
  [ "$status" -eq 0 ]
  [ "$output" = "RESULT: BLOCKED" ]

  # Target remains untouched
  target_content=$(cat "$target")
  [ "$target_content" = "ORIGINAL_SECRET" ]
}

@test "mktemp creates regular file with 0600 mode" {
  # shellcheck disable=SC2016 # expressions belong to shell under test
  run_lab_c '
    tmpfile=$(mktemp "$1/file.XXXXXX") || exit 1
    # Check that permissions start with -rw------- (ignoring macOS @ or + ACL suffix)
    perms=$(ls -l "$tmpfile" | awk "{print \$1}")
    rm -f "$tmpfile"
    case $perms in
      -rw-------*) echo "VALID_0600" ;;
      *) echo "INVALID: $perms" ;;
    esac
  ' sh "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "VALID_0600" ]
}

@test "mktemp -d creates directory with 0700 mode" {
  # shellcheck disable=SC2016 # expressions belong to shell under test
  run_lab_c '
    tmpdir=$(mktemp -d "$1/dir.XXXXXX") || exit 1
    # Check that permissions start with drwx------ (ignoring macOS @ or + ACL suffix)
    perms=$(ls -ld "$tmpdir" | awk "{print \$1}")
    rm -rf "$tmpdir"
    case $perms in
      drwx------*) echo "VALID_0700" ;;
      *) echo "INVALID: $perms" ;;
    esac
  ' sh "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "VALID_0700" ]
}

@test "secure_tempdir.sh cleans up directory on exit via trap" {
  run_lab "$SECURE" "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [[ "$output" =~ "DIR_CREATED: " ]]
  [[ "$output" =~ "PAYLOAD: PROTECTED_DATA" ]]

  # Extract the created directory path from output
  dir_path=$(printf '%s\n' "$output" | awk '/DIR_CREATED: / {print $2}')
  [ -n "$dir_path" ]
  # Assert directory has been cleaned up by EXIT trap
  [ ! -d "$dir_path" ]
}

@test "pre-created FIFO with peer demonstrates data interception" {
  fifo_path="$BATS_TEST_TMPDIR/probe_fifo.tmp"
  mkfifo "$fifo_path"

  # Start guaranteed background reader with timeout
  # Read from FIFO and write to captured log
  log_path="$BATS_TEST_TMPDIR/intercepted.log"
  (
    # Read one line and save
    if read -r line <"$fifo_path"; then
      printf '%s\n' "$line" >"$log_path"
    fi
  ) &
  reader_pid=$!

  # Run vulnerable script pointing at the FIFO
  run_lab "$VULN" "$BATS_TEST_TMPDIR" "probe_fifo"
  [ "$status" -eq 0 ]

  # Wait for background reader to complete
  wait "$reader_pid" || true

  # Verify intercepted payload
  intercepted=$(cat "$log_path")
  [ "$intercepted" = "CRITICAL_PAYLOAD_DATA" ]

  rm -f "$fifo_path" "$log_path"
}
