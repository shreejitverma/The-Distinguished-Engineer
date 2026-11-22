#!/usr/bin/env bats
# links.bats - test suite for Inodes and Links

setup() {
  load ../lib/lab
  LAB_DIR="$BATS_TEST_DIRNAME"
  PROBE_BIN="$BATS_TEST_TMPDIR/probe"
  if [ ! -x "$PROBE_BIN" ]; then
    "${CC:-cc}" -Wall -Wextra -Werror -o "$PROBE_BIN" "$LAB_DIR/probe.c"
  fi
}

# skip_without_ef: posh 0.14.1 has no `-ef` (measured: "[: -ef: unexpected operator/operand").
skip_without_ef() {
  local family
  family=$(lab_shell_family)
  [ "$family" != posh ] || skip "posh has no test -ef"
}

@test "hard links share file identity and data modification" {
  skip_without_ef
  mkdir -p "$BATS_TEST_TMPDIR/t1"
  run_lab "$LAB_DIR/link_probe.sh" hardlink "$BATS_TEST_TMPDIR/t1"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ef_hardlink=true" ]
  [ "${lines[1]}" = "read_via_base=updated via link2" ]
  [ "${lines[2]}" = "surviving_content=updated via link2" ]
}

@test "test -ef across shells evaluates hard links vs distinct files" {
  skip_without_ef
  mkdir -p "$BATS_TEST_TMPDIR/t2"
  # shellcheck disable=SC2016 # the script is expanded by the lab shell; only the test paths are spliced in
  run_lab_c '
    dir="'"$BATS_TEST_TMPDIR"'/t2"
    printf "alpha\n" >"$dir/a.txt"
    ln "$dir/a.txt" "$dir/b.txt"
    printf "alpha\n" >"$dir/c.txt"

    [ "$dir/a.txt" -ef "$dir/b.txt" ] || exit 11
    [ ! "$dir/a.txt" -ef "$dir/c.txt" ] || exit 12
    echo "test_ef=ok"
  '
  [ "$status" -eq 0 ]
  [ "$output" = "test_ef=ok" ]
}

@test "symlink classification distinguishes symlinks from targets and dangling links" {
  skip_without_ef
  mkdir -p "$BATS_TEST_TMPDIR/t3"
  run_lab "$LAB_DIR/link_probe.sh" symlink "$BATS_TEST_TMPDIR/t3"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "valid_symlink: -h=yes -L=yes -e=yes -ef_target=yes" ]
  [ "${lines[1]}" = "broken_symlink: -h=yes -L=yes -e=no -ef_self=no" ]
}

@test "trailing slash forces symlink resolution to directory and rejects regular files" {
  mkdir -p "$BATS_TEST_TMPDIR/t4"
  run_lab "$LAB_DIR/link_probe.sh" trailing_slash "$BATS_TEST_TMPDIR/t4"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "file_slash=enotdir" ]
  [ "${lines[1]}" = "dir_slash=directory" ]
}

@test "atomic symlink swap updates directory pointer without descending" {
  mkdir -p "$BATS_TEST_TMPDIR/t5"
  run_lab "$LAB_DIR/link_probe.sh" atomic_swap "$BATS_TEST_TMPDIR/t5"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "initial_version=version 1" ]
  case "${lines[1]}" in
    swap_mode=gnu_T | swap_mode=bsd_h) ;;
    *) false ;;
  esac
  [ "${lines[2]}" = "swapped_version=version 2" ]
}

@test "unlinked open file keeps data blocks accessible until close" {
  mkdir -p "$BATS_TEST_TMPDIR/t6"
  run "$PROBE_BIN" unlinked "$BATS_TEST_TMPDIR/t6"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "before_nlink=1" ]
  [ "${lines[1]}" = "after_nlink=0" ]
  [ "${lines[2]}" = "bytes_read=21" ]
  [ "${lines[3]}" = "payload=active inode payload" ]
}

@test "kernel forbids creating hard links to directories" {
  mkdir -p "$BATS_TEST_TMPDIR/t7/src_dir"
  run "$PROBE_BIN" link_dir "$BATS_TEST_TMPDIR/t7/src_dir" "$BATS_TEST_TMPDIR/t7/dest_dir"
  [ "$status" -eq 0 ]
  case "$output" in
    errno=EPERM | errno=EISDIR | errno=EOPNOTSUPP) ;;
    *) false ;;
  esac
}

@test "symlink loop returns ELOOP upon path resolution" {
  mkdir -p "$BATS_TEST_TMPDIR/t8"
  run "$PROBE_BIN" symloop "$BATS_TEST_TMPDIR/t8"
  [ "$status" -eq 0 ]
  [ "$output" = "errno=ELOOP" ]
}

@test "readlink returns raw target without null termination" {
  mkdir -p "$BATS_TEST_TMPDIR/t9"
  ln -s "sample_target_string" "$BATS_TEST_TMPDIR/t9/sym"
  run "$PROBE_BIN" readlink "$BATS_TEST_TMPDIR/t9/sym"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "target=sample_target_string" ]
  [ "${lines[1]}" = "len=20" ]
  [ "${lines[2]}" = "nul_written=no" ]
}

@test "lstat vs stat distinguishes symlink inode from target inode" {
  mkdir -p "$BATS_TEST_TMPDIR/t10"
  printf 'hello world\n' >"$BATS_TEST_TMPDIR/t10/file.txt"
  ln -s file.txt "$BATS_TEST_TMPDIR/t10/file.sym"

  run "$PROBE_BIN" stat_ids "$BATS_TEST_TMPDIR/t10/file.sym"
  [ "$status" -eq 0 ]

  # lstat must report symlink type (1) and stat must report non-symlink target (0)
  # shellcheck disable=SC2016 # the script is expanded by the lab shell; only the test output is spliced in
  run_lab_c '
    out="'"$output"'"
    is_lnk=$(printf "%s\n" "$out" | grep "^lstat_is_lnk=1")
    is_tgt=$(printf "%s\n" "$out" | grep "^stat_is_lnk=0")
    [ -n "$is_lnk" ] && [ -n "$is_tgt" ] && echo "ids=ok"
  '
  [ "$status" -eq 0 ]
  [ "$output" = "ids=ok" ]
}
