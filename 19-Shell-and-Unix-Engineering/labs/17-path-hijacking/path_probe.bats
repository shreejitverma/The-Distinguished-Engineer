#!/usr/bin/env bats
# Tests for path_probe.sh and PATH hijacking mechanisms.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/path_probe.sh"
}

@test "audit flags empty PATH string as danger" {
  run_lab "$PROBE" audit ""
  [ "$status" -eq 0 ]
  [ "$output" = "DANGER: PATH is empty (resolves to current working directory)" ]
}

@test "audit flags leading colon, trailing colon, and double colon as empty elements" {
  run_lab "$PROBE" audit ":/usr/bin::/bin:"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "DANGER: empty element in PATH (resolves to cwd)" ]
  [ "${lines[1]}" = 'OK: "/usr/bin"' ]
  [ "${lines[2]}" = "DANGER: empty element in PATH (resolves to cwd)" ]
  [ "${lines[3]}" = 'OK: "/bin"' ]
  [ "${lines[4]}" = "DANGER: empty element in PATH (resolves to cwd)" ]
}

@test "audit flags relative directory and explicit dot as danger" {
  run_lab "$PROBE" audit ".:./local/bin:local/bin:/usr/bin"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = 'DANGER: explicit dot "." in PATH' ]
  [ "${lines[1]}" = 'DANGER: relative directory "./local/bin" in PATH' ]
  [ "${lines[2]}" = 'DANGER: relative directory "local/bin" in PATH' ]
  [ "${lines[3]}" = 'OK: "/usr/bin"' ]
}

@test "audit flags world-writable directory without sticky bit" {
  local bad_dir="$BATS_TEST_TMPDIR/world_writable"
  mkdir "$bad_dir"
  chmod 777 "$bad_dir"

  run_lab "$PROBE" audit "/usr/bin:$bad_dir"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = 'OK: "/usr/bin"' ]
  [ "${lines[1]}" = "DANGER: directory \"$bad_dir\" is world-writable without sticky bit" ]
}

@test "resolve detects local executable hijacking when PATH contains leading colon" {
  local local_bin="$BATS_TEST_TMPDIR/ls"
  printf '#!/bin/sh\necho ROGUE\n' >"$local_bin"
  chmod +x "$local_bin"

  cd "$BATS_TEST_TMPDIR"
  run_lab "$PROBE" resolve ls ":/bin:/usr/bin"
  [ "$status" -eq 0 ]
  [ "$output" = "HIJACKED: ./ls" ]
}

@test "resolve finds absolute system binary when PATH is clean" {
  cd "$BATS_TEST_TMPDIR"
  run_lab "$PROBE" resolve ls "/bin:/usr/bin"
  [ "$status" -eq 0 ]
  case "$output" in
    "RESOLVED: /bin/ls" | "RESOLVED: /usr/bin/ls") ;;
    *) false ;;
  esac
}

@test "sanitize strips dangerous elements and keeps safe directories" {
  local bad_dir="$BATS_TEST_TMPDIR/untrusted"
  mkdir "$bad_dir"
  chmod 777 "$bad_dir"

  run_lab "$PROBE" sanitize ":/usr/bin:.:./bin:$bad_dir:/bin:"
  [ "$status" -eq 0 ]
  [ "$output" = "/usr/bin:/bin" ]
}

@test "empty PATH executes local executable across POSIX shells except ksh93" {
  local probe_bin="$BATS_TEST_TMPDIR/local_probe"
  printf '#!/bin/sh\necho LOCAL_PROBE_CALLED\n' >"$probe_bin"
  chmod +x "$probe_bin"

  cd "$BATS_TEST_TMPDIR"
  # shellcheck disable=SC2016 # intentional execution inside shell under test
  run_lab_c 'PATH=""; local_probe'
  case $(lab_shell_family) in
    ksh93)
      [ "$status" -eq 127 ]
      ;;
    bash | zsh | zsh-sh | dash | yash | busybox | posh | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = "LOCAL_PROBE_CALLED" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "shell hash table retains initial location until PATH reassignment" {
  local dir1="$BATS_TEST_TMPDIR/hdir1"
  local dir2="$BATS_TEST_TMPDIR/hdir2"
  mkdir -p "$dir1" "$dir2"

  local victim2="$dir2/victim_cmd"
  printf '#!/bin/sh\necho ORIGINAL\n' >"$victim2"
  chmod +x "$victim2"

  cd "$BATS_TEST_TMPDIR"
  # shellcheck disable=SC2016 # testing hash invalidation upon PATH reassignment
  run_lab_c '
    PATH="'"$dir1"':'"$dir2"':/bin:/usr/bin"
    victim_cmd
    printf "#!/bin/sh\necho HIJACKED\n" > "'"$dir1"'/victim_cmd"
    chmod +x "'"$dir1"'/victim_cmd"
    victim_cmd
    PATH="$PATH"
    victim_cmd
  '
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | yash | busybox | mksh)
      [ "${lines[0]}" = "ORIGINAL" ]
      [ "${lines[1]}" = "ORIGINAL" ]
      [ "${lines[2]}" = "HIJACKED" ]
      ;;
    ksh93 | posh)
      # ksh93 and posh 0.14.1 find the new earlier file without a PATH assignment.
      [ "${lines[0]}" = "ORIGINAL" ]
      [ "${lines[1]}" = "HIJACKED" ]
      [ "${lines[2]}" = "HIJACKED" ]
      ;;
    *)
      false
      ;;
  esac
}
