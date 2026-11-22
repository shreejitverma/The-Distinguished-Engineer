#!/usr/bin/env bats
# Tests for notify_probe.c (what inotify(7) on Linux and kqueue(2) EVFILT_VNODE on macOS
# and the BSDs report for the same filesystem operations) and snapdiff.sh (the portable
# find + cksum polling fallback, run under the shell under test).
# Kernel behavior does not depend on the shell under test, so the probe's expected output
# branches on the backend the probe itself reports, and that backend is checked against
# the running kernel (uname -s). snapdiff.sh is POSIX sh and must behave the same in every
# shell family.
# shellcheck disable=SC2016 # single-quoted code belongs to the shell under test

setup() {
  load ../lib/lab
  SNAPDIFF="$BATS_TEST_DIRNAME/snapdiff.sh"
  cd "$BATS_TEST_TMPDIR" || return 1
}

# build_probe: compile notify_probe.c into the test's scratch directory and set BACKEND;
# a missing compiler fails the test.
build_probe() {
  PROBE="$BATS_TEST_TMPDIR/notify_probe"
  "${CC:-cc}" -std=c11 -Wall -Wextra -Werror -o "$PROBE" "$BATS_TEST_DIRNAME/notify_probe.c"
  BACKEND=$("$PROBE" backend)
}

@test "the probe uses inotify on Linux and kqueue on Darwin and the BSDs" {
  build_probe
  kernel=$(uname -s)
  case $kernel in
    Linux) [ "$BACKEND" = inotify ] ;;
    Darwin | FreeBSD | NetBSD | OpenBSD) [ "$BACKEND" = kqueue ] ;;
    *) false ;;
  esac
}

@test "a directory watch: inotify names every child event, kqueue says only that the directory changed" {
  build_probe
  mkdir d
  run_lab_c '"$1" dir "$2"' sh "$PROBE" d
  [ "$status" -eq 0 ]
  case $BACKEND in
    inotify)
      [ "${lines[0]}" = "CREATE a.txt" ]
      [ "${lines[1]}" = "MODIFY a.txt" ]
      [ "${lines[2]}" = "CLOSE_WRITE a.txt" ]
      [ "${lines[3]}" = "MOVED_FROM a.txt" ]
      [ "${lines[4]}" = "MOVED_TO b.txt" ]
      [ "${lines[5]}" = "CREATE|ISDIR sub" ]
      [ "${lines[6]}" = "DELETE b.txt" ]
      [ "${lines[7]}" = "DELETE|ISDIR sub" ]
      [ "${lines[8]}" = "events: 8" ]
      [ "${lines[9]}" = "rename cookies: equal" ]
      [ "${#lines[@]}" -eq 10 ]
      ;;
    kqueue)
      # Five operations on the directory collapse into one knote; no names at all.
      [ "${lines[0]}" = "WRITE|LINK -" ]
      [ "${lines[1]}" = "events: 1" ]
      [ "${#lines[@]}" -eq 2 ]
      ;;
    *) false ;;
  esac
}

@test "unread identical events coalesce, so neither API can count writes" {
  build_probe
  run_lab_c '"$1" coalesce "$2"' sh "$PROBE" f
  [ "$status" -eq 0 ]
  case $BACKEND in
    inotify) [ "${lines[0]}" = "5 writes, read once: 1 event(s) [MODIFY]" ] ;;
    kqueue) [ "${lines[0]}" = "5 writes, read once: 1 event(s) [WRITE|EXTEND]" ] ;;
    *) false ;;
  esac
  [ "${lines[1]}" = "2 writes, read after each: 2 event(s)" ]
}

@test "inotify drops events past max_queued_events and appends one IN_Q_OVERFLOW; kqueue never overflows" {
  build_probe
  mkdir d
  run_lab_c '"$1" overflow "$2"' sh "$PROBE" d
  [ "$status" -eq 0 ]
  case $BACKEND in
    inotify)
      case ${lines[0]} in
        "max_queued_events: "[1-9]*) ;;
        *) false ;;
      esac
      case ${lines[1]} in
        "generated: limit + 199" | "generated: limit + 200") ;;
        *) false ;;
      esac
      [ "${lines[2]}" = "read: limit + 1" ]
      [ "${lines[3]}" = "last: Q_OVERFLOW wd=-1" ]
      ;;
    kqueue)
      [ "${lines[0]}" = "generated: 20000" ]
      [ "${lines[1]}" = "read: 1" ]
      [ "${lines[2]}" = "last: WRITE" ]
      ;;
    *) false ;;
  esac
}

@test "an editor-style save (rename over) kills a file watch; later writes go unseen" {
  build_probe
  mkdir d
  run_lab_c '"$1" replace "$2"' sh "$PROBE" d
  [ "$status" -eq 0 ]
  case $BACKEND in
    inotify) [ "${lines[0]}" = "after rename over target: ATTRIB DELETE_SELF IGNORED" ] ;;
    kqueue) [ "${lines[0]}" = "after rename over target: DELETE" ] ;;
    *) false ;;
  esac
  [ "${lines[1]}" = "after rewriting the new target: none" ]
  # The rewrite really happened: the path now names a file holding v3.
  content=$(cat d/target)
  [ "$content" = v3 ]
}

@test "read -d '' (POSIX.1-2024) splits a NUL-separated event stream such as fswatch -0" {
  # Two records; the second contains a newline, as a hostile file name can.
  run_lab_c 'printf "a b\000c\nd\000" | {
    n=0
    while IFS= read -r -d "" f; do
      n=$((n + 1))
      printf "<%s>" "$f"
    done
    echo " n=$n"
  }'
  family=$(lab_shell_family)
  version=$(lab_shell_version)
  expect=two
  case $family in
    bash | zsh | zsh-sh | busybox | mksh) ;;
    dash | posh) expect=none ;;
    yash)
      # read -d arrived in yash 2.59 (NEWS); 2.58.1 rejects the option.
      case $version in
        1.* | 2.[0-9] | 2.[0-9].* | 2.[1-4][0-9] | 2.[1-4][0-9].* | 2.5[0-8] | 2.5[0-8].*)
          expect=none
          ;;
        *) ;;
      esac
      ;;
    ksh93)
      # ksh93u+ 2012 loses the NUL boundary and keeps one mangled record;
      # ksh93u+m 1.0.10 splits correctly (measured in the matrix).
      case $version in
        *93u+m*) ;;
        *) expect=mangled ;;
      esac
      ;;
    *) false ;;
  esac
  case $expect in
    two)
      [ "$output" = "<a b><c
d> n=2" ]
      ;;
    none)
      # No -d option: read fails at once, so the loop body never runs.
      case $output in
        *" n=0") ;;
        *) false ;;
      esac
      ;;
    mangled) [ "$output" = "<a bc> n=1" ] ;;
    *) false ;;
  esac
}

@test "snapdiff reports added, deleted, and modified files" {
  mkdir d
  printf 'one\n' >d/keep
  printf 'two\n' >d/gone
  printf 'three\n' >d/edit
  run_lab "$SNAPDIFF" snap d
  [ "$status" -eq 0 ]
  printf '%s\n' "$output" >old
  rm d/gone
  printf 'THREE!\n' >d/edit
  printf 'new\n' >d/added
  run_lab "$SNAPDIFF" snap d
  [ "$status" -eq 0 ]
  printf '%s\n' "$output" >new
  run_lab "$SNAPDIFF" diff old new
  [ "$status" -eq 0 ]
  # Sorted by path, not by kind of change.
  [ "$output" = "A d/added
M d/edit
D d/gone" ]
}

@test "snapdiff catches a same-size rewrite by its checksum" {
  mkdir d
  printf 'aaaa\n' >d/f
  run_lab "$SNAPDIFF" snap d
  printf '%s\n' "$output" >old
  printf 'bbbb\n' >d/f
  run_lab "$SNAPDIFF" snap d
  printf '%s\n' "$output" >new
  # Same size before and after, so a size-only (or size plus coarse mtime) poll misses it.
  old_size=$(awk '{ print $2 }' old)
  new_size=$(awk '{ print $2 }' new)
  [ "$old_size" = 5 ]
  [ "$new_size" = 5 ]
  run_lab "$SNAPDIFF" diff old new
  [ "$status" -eq 0 ]
  [ "$output" = "M d/f" ]
}

@test "snapdiff keeps names with spaces and a leading dash intact" {
  mkdir d
  run_lab "$SNAPDIFF" snap d
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  : >old
  printf 'x\n' >'d/-x  two spaces.txt'
  run_lab "$SNAPDIFF" snap d
  printf '%s\n' "$output" >new
  run_lab "$SNAPDIFF" diff old new
  [ "$status" -eq 0 ]
  [ "$output" = "A d/-x  two spaces.txt" ]
}

@test "snapdiff refuses a file name that contains a newline" {
  mkdir d
  : >"d/bad
name"
  run_lab "$SNAPDIFF" snap d
  [ "$status" -eq 3 ]
  case $output in
    *"contains a newline"*) ;;
    *) false ;;
  esac
}

@test "snapdiff rejects bad usage with status 2" {
  run_lab "$SNAPDIFF" watch d
  [ "$status" -eq 2 ]
  run_lab "$SNAPDIFF" snap no-such-dir
  [ "$status" -eq 2 ]
  run_lab "$SNAPDIFF" diff only-one
  [ "$status" -eq 2 ]
}
