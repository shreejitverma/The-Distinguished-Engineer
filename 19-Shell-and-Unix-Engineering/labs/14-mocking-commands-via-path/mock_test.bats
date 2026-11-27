#!/usr/bin/env bats
# Tests for command mocking via PATH manipulation.

setup() {
  load ../lib/lab
  SYNC_SERVICE="$BATS_TEST_DIRNAME/sync_service.sh"
  MOCK_DIR="$BATS_TEST_TMPDIR/mock_bin"
  mkdir -p "$MOCK_DIR"
  ORIG_PATH="$PATH"
}

teardown() {
  PATH="$ORIG_PATH"
  rm -rf "$MOCK_DIR"
}

@test "successful command interception via PATH mock" {
  cat <<'EOF' >"$MOCK_DIR/curl"
#!/bin/sh
printf '{"status":"uploaded","id":42}\n'
exit 0
EOF
  chmod +x "$MOCK_DIR/curl"

  cat <<'EOF' >"$MOCK_DIR/date"
#!/bin/sh
printf '1700000000\n'
exit 0
EOF
  chmod +x "$MOCK_DIR/date"

  cat <<'EOF' >"$MOCK_DIR/logger"
#!/bin/sh
exit 0
EOF
  chmod +x "$MOCK_DIR/logger"

  # shellcheck disable=SC2030,SC2031 # PATH modification intentionally scoped to bats subshell test
  PATH="$MOCK_DIR:$PATH"
  run_lab "$SYNC_SERVICE" "https://api.example.com/v1/sync" "payload-bytes"
  [ "$status" -eq 0 ]
  [ "$output" = 'SUCCESS: {"status":"uploaded","id":42} (timestamp: 1700000000)' ]
}

@test "mock spy records invocation arguments and environment" {
  local log_file="$BATS_TEST_TMPDIR/curl_spy.log"
  cat <<EOF >"$MOCK_DIR/curl"
#!/bin/sh
printf '%s\n' "\$@" > "$log_file"
printf '{"status":"ok"}\n'
exit 0
EOF
  chmod +x "$MOCK_DIR/curl"

  cat <<'EOF' >"$MOCK_DIR/date"
#!/bin/sh
printf '1700000000\n'
exit 0
EOF
  chmod +x "$MOCK_DIR/date"

  # shellcheck disable=SC2030,SC2031 # PATH modification intentionally scoped to bats subshell test
  PATH="$MOCK_DIR:$PATH"
  run_lab "$SYNC_SERVICE" "https://api.example.com/v1/sync" "payload-with-spaces and special 'chars'"
  [ "$status" -eq 0 ]
  [ -f "$log_file" ]

  # Verify the recorded arguments match the exact command invocation in sync_service.sh
  run_lab_c "head -n 1 '$log_file'"
  [ "$output" = "-s" ]

  run_lab_c "tail -n 1 '$log_file'"
  [ "$output" = "https://api.example.com/v1/sync" ]
}

@test "mock simulates failure exit code and diagnostic stderr" {
  cat <<'EOF' >"$MOCK_DIR/curl"
#!/bin/sh
echo "curl: (22) The requested URL returned error: 503 Service Unavailable" >&2
exit 22
EOF
  chmod +x "$MOCK_DIR/curl"

  cat <<'EOF' >"$MOCK_DIR/date"
#!/bin/sh
printf '1700000000\n'
exit 0
EOF
  chmod +x "$MOCK_DIR/date"

  # shellcheck disable=SC2030,SC2031 # PATH modification intentionally scoped to bats subshell test
  PATH="$MOCK_DIR:$PATH"
  run_lab "$SYNC_SERVICE" "https://api.example.com/v1/sync" "error-payload"
  [ "$status" -eq 2 ]
  [[ "$output" =~ "503 Service Unavailable" ]]
}

@test "non-executable file in early PATH directory is skipped in search" {
  local dir1="$BATS_TEST_TMPDIR/d1"
  local dir2="$BATS_TEST_TMPDIR/d2"
  mkdir -p "$dir1" "$dir2"

  # Create tool in dir1 without executable permission
  cat <<'EOF' >"$dir1/test_fallback"
#!/bin/sh
echo "FAIL_DIR1"
EOF
  chmod -x "$dir1/test_fallback"

  # Create tool in dir2 with executable permission
  cat <<'EOF' >"$dir2/test_fallback"
#!/bin/sh
echo "SUCCESS_DIR2"
EOF
  chmod +x "$dir2/test_fallback"

  # Run inline script under tested shell
  # shellcheck disable=SC2016
  run_lab_c '
    d1="$1"; d2="$2"
    PATH="$d1:$d2:$PATH"
    test_fallback
  ' inline-test "$dir1" "$dir2"

  [ "$status" -eq 0 ]
  [ "$output" = "SUCCESS_DIR2" ]
}

@test "command hashing caches first lookup until hash table is cleared" {
  local dir1="$BATS_TEST_TMPDIR/hash1"
  local dir2="$BATS_TEST_TMPDIR/hash2"
  mkdir -p "$dir1" "$dir2"

  cat <<'EOF' >"$dir2/probe_cmd"
#!/bin/sh
echo "INITIAL"
EOF
  chmod +x "$dir2/probe_cmd"

  # Every probe_cmd runs in the current shell (output to a file, not $(...)),
  # because a lookup made inside a command substitution subshell is remembered
  # only by that subshell and would never reach the parent's hash table.
  # shellcheck disable=SC2016
  run_lab_c '
    dir1="$1"; dir2="$2"; out="$3"
    PATH="$dir1:$dir2:$PATH"
    probe_cmd >"$out.1"

    # Place a shadowing binary in dir1 after probe_cmd was already remembered
    cat << "INNER" > "$dir1/probe_cmd"
#!/bin/sh
echo "SHADOWED"
INNER
    chmod +x "$dir1/probe_cmd"

    # Second lookup without clearing the hash table
    probe_cmd >"$out.2"

    # Clear the hash table via hash -r, or PATH reassignment where hash is absent (posh)
    hash -r 2>/dev/null || PATH="$PATH"
    probe_cmd >"$out.3"

    printf "%s -> %s -> %s\n" "$(cat "$out.1")" "$(cat "$out.2")" "$(cat "$out.3")"
  ' inline-hash "$dir1" "$dir2" "$BATS_TEST_TMPDIR/hashres"

  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    ksh93 | posh)
      # ksh93 (93u+ and 93u+m) and posh re-search PATH here, so the new file wins at once
      [ "$output" = "INITIAL -> SHADOWED -> SHADOWED" ]
      ;;
    *)
      # bash, dash, zsh, BusyBox ash, mksh, yash keep the remembered location until it is cleared
      [ "$output" = "INITIAL -> INITIAL -> SHADOWED" ]
      ;;
  esac
}

@test "regular built-in utility takes precedence over PATH mock" {
  # Create a mock named printf in MOCK_DIR
  cat <<'EOF' >"$MOCK_DIR/printf"
#!/bin/sh
echo "MOCK_PRINTF"
EOF
  chmod +x "$MOCK_DIR/printf"

  # shellcheck disable=SC2016
  run_lab_c '
    mock_dir="$1"
    PATH="$mock_dir:$PATH"
    printf "BUILTIN_EXEC"
  ' inline-builtin "$MOCK_DIR"

  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    mksh | posh)
      # mksh and posh have no printf built-in, so the PATH mock is found and runs
      [ "$output" = "MOCK_PRINTF" ]
      ;;
    *)
      [ "$output" = "BUILTIN_EXEC" ]
      ;;
  esac
}

@test "absolute pathname bypasses PATH search completely" {
  # Mock echo in MOCK_DIR
  cat <<'EOF' >"$MOCK_DIR/echo"
#!/bin/sh
printf 'MOCK_ECHO\n'
EOF
  chmod +x "$MOCK_DIR/echo"

  # Invoking /bin/echo directly with a slash bypasses PATH search (POSIX.1-2024 XCU 2.9.1.4)
  # shellcheck disable=SC2016
  run_lab_c '
    mock_dir="$1"
    PATH="$mock_dir:$PATH"
    /bin/echo "BYPASS_TEST"
  ' inline-absolute "$MOCK_DIR"

  [ "$status" -eq 0 ]
  [ "$output" = "BYPASS_TEST" ]
}
