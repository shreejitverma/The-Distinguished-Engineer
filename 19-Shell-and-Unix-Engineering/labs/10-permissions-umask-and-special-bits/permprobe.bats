#!/usr/bin/env bats
# Tests for permission bits, umask calculations, and special bits.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/permprobe.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
  export GETMODE_BIN="$BATS_TEST_TMPDIR/getmode"
  "${CC:-cc}" -O2 -o "$GETMODE_BIN" "$BATS_TEST_DIRNAME/getmode.c"
}

@test "umask calculation clears requested bits: 0666 & ~umask and 0777 & ~umask" {
  run_lab "$PROBE" umask-calc
  [ "$status" -eq 0 ]
  [ "$output" = "u022_file=0644 u022_dir=0755 u077_file=0600 u077_dir=0700 u027_file=0640 u027_dir=0750" ]
}

@test "umask output format: symbolic mode conforms to POSIX and octal digits vary by shell" {
  local family octal symbolic p_status
  family=$(lab_shell_family)

  run_lab "$PROBE" umask-format
  [ "$status" -eq 0 ]

  # Parse key=value fields from output
  octal=$(printf '%s\n' "$output" | sed -n 's/.*octal=\([^ ]*\).*/\1/p')
  symbolic=$(printf '%s\n' "$output" | sed -n 's/.*symbolic=\([^ ]*\).*/\1/p')
  p_status=$(printf '%s\n' "$output" | sed -n 's/.*p_status=\([^ ]*\).*/\1/p')

  [ "$symbolic" = "u=rwx,g=rx,o=rx" ]

  # Measured in the shell matrix: only bash accepts `umask -p`; zsh, mksh, and posh print three digits.
  case $family in
    bash)
      [ "$octal" = "0022" ]
      [ "$p_status" -eq 0 ]
      ;;
    zsh | zsh-sh | mksh | posh)
      [ "$octal" = "022" ]
      [ "$p_status" -ne 0 ]
      ;;
    ksh93 | dash | yash | busybox)
      [ "$octal" = "0022" ]
      [ "$p_status" -ne 0 ]
      ;;
    *)
      false
      ;;
  esac
}

@test "negative permissions: matching file owner class excludes group and other bits" {
  run_lab "$PROBE" negative-perms
  [ "$status" -eq 0 ]
  [ "$output" = "owner_read_rc=1 owner_write_rc=1" ]
}

@test "directory permissions: r without x forbids traversal, x without r allows known path, deletion depends on dir w+x" {
  run_lab "$PROBE" dir-perms
  [ "$status" -eq 0 ]
  [ "$output" = "read_only_traversal_rc=1 exec_only_read_rc=0 rm_mode0_rc=0" ]
}

@test "sticky bit S_ISVTX is stored on a directory (1777) and the directory owner can still unlink its own file" {
  run_lab "$PROBE" sticky-dir
  [ "$status" -eq 0 ]
  [ "$output" = "sticky_mode=1777 dir_owner_unlink_rc=0" ]
}

@test "kernel drops S_ISUID on unprivileged write" {
  run_lab "$PROBE" suid-drop
  [ "$status" -eq 0 ]
  [ "$output" = "before=4755 after=0755" ]
}

@test "chmod without who filters perms through umask, while explicit who=a bypasses it" {
  run_lab "$PROBE" chmod-umask
  [ "$status" -eq 0 ]
  [ "$output" = "no_who=0744 explicit_who=0755" ]
}

@test "chmod capital X applies execute only to directories and already-executable files" {
  run_lab "$PROBE" chmod-capital-x
  [ "$status" -eq 0 ]
  [ "$output" = "dir=0755 data=0644 bin=0755" ]
}
