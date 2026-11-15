#!/usr/bin/env bats
# Tests for fsprobe.c (what rename(2), fsync(2), fdatasync(2), and F_FULLFSYNC do) and
# durable.sh (durable_replace, the write, flush, rename, flush-directory protocol in sh),
# plus how the sync and dd utilities of each userland treat file operands and pipes.
# Kernel and utility behavior does not depend on the shell under test, so the expected
# values branch on the operating system (uname -s) or on the utility implementation.
# shellcheck disable=SC2016 # single-quoted code belongs to the shell under test

setup() {
  load ../lib/lab
  DURABLE="$BATS_TEST_DIRNAME/durable.sh"
  W="$BATS_TEST_TMPDIR/w"
  mkdir "$W"
}

# build_probe: compile fsprobe.c into the test's scratch directory, plus a link named
# "fsync" that runs its fsync(1) mode; a missing compiler fails the test.
build_probe() {
  PROBE="$BATS_TEST_TMPDIR/fsprobe"
  FSYNC_TOOL="$BATS_TEST_TMPDIR/fsync"
  "${CC:-cc}" -std=c99 -Wall -Wextra -Werror -o "$PROBE" "$BATS_TEST_DIRNAME/fsprobe.c"
  ln -s "$PROBE" "$FSYNC_TOOL"
}

# sync_impl: gnu, busybox, uutils, or bsd (macOS sync takes no options: --version prints
# nothing and exits 0); anything else is unknown and fails the tests that branch on it.
sync_impl() {
  local out rc=0
  out=$(sync --version 2>&1 </dev/null) || rc=$?
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    "") if [ "$rc" -eq 0 ]; then echo bsd; else echo unknown; fi ;;
    *) echo unknown ;;
  esac
}

# dd_impl: gnu, busybox, uutils, or bsd (BSD and macOS dd reject --version as an operand).
dd_impl() {
  local out
  out=$(dd --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"unknown operand"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# count_named DIR PATTERN: print how many files under DIR have a name matching PATTERN.
count_named() {
  local list name n=0
  list=$(find "$1" -name "$2")
  while IFS= read -r name; do
    if [ -n "$name" ]; then n=$((n + 1)); fi
  done <<<"$list"
  echo "$n"
}

@test "features: F_FULLFSYNC and F_BARRIERFSYNC exist on macOS only; fdatasync on both" {
  build_probe
  run_lab_c '"$1" features' sh "$PROBE"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Darwin) [ "$output" = "fdatasync=yes full_fsync=yes barrier_fsync=yes" ] ;;
    Linux) [ "$output" = "fdatasync=yes full_fsync=no barrier_fsync=no" ] ;;
    *) false ;;
  esac
}

@test "dirfd: a directory cannot be opened for writing (EISDIR) but fsyncs through O_RDONLY" {
  build_probe
  run_lab_c '"$1" dirfd "$2"' sh "$PROBE" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = "wronly=EISDIR rdonly_fsync=ok rdonly_fdatasync=ok" ]
}

@test "rdonly: fsync and fdatasync work through a read-only descriptor" {
  build_probe
  printf 'data\n' >"$W/f"
  chmod 444 "$W/f"
  run_lab_c '"$1" rdonly "$2"' sh "$PROBE" "$W/f"
  [ "$status" -eq 0 ]
  [ "$output" = "rdonly_fsync=ok rdonly_fdatasync=ok" ]
}

@test "pipe: fsync and fdatasync on a pipe fail with EINVAL" {
  build_probe
  run_lab_c '"$1" pipe' sh "$PROBE"
  [ "$status" -eq 0 ]
  [ "$output" = "fsync=EINVAL fdatasync=EINVAL" ]
}

@test "samefile: rename between two hard links of one file succeeds and removes neither" {
  build_probe
  run_lab_c '"$1" samefile "$2"' sh "$PROBE" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = "rename=ok a=yes b=yes nlink=2" ]
}

@test "replace: the four-step protocol keeps the temporary file's inode under the new name" {
  build_probe
  printf 'OLD\n' >"$W/dest"
  run_lab_c '
    for m in none fdatasync fsync; do
      "$1" replace "$2" dest "NEW-$m" "$m" || exit
      printf "%s\n" "$(cat "$2/dest")"
    done' sh "$PROBE" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = "flush=none:ok rename=ok ino_kept=yes dirsync=skipped
NEW-none
flush=fdatasync:ok rename=ok ino_kept=yes dirsync=ok
NEW-fdatasync
flush=fsync:ok rename=ok ino_kept=yes dirsync=ok
NEW-fsync" ]
  left=$(count_named "$W" '.rf.*')
  [ "$left" = 0 ]
}

@test "replace full: F_FULLFSYNC replaces on macOS; elsewhere status 2 and the old file stays" {
  build_probe
  printf 'OLD\n' >"$W/dest"
  run_lab_c '"$1" replace "$2" dest NEW full' sh "$PROBE" "$W"
  content=$(cat "$W/dest")
  case $(uname -s) in
    Darwin)
      [ "$status" -eq 0 ]
      [ "$output" = "flush=full:ok rename=ok ino_kept=yes dirsync=ok" ]
      [ "$content" = NEW ]
      ;;
    Linux)
      [ "$status" -eq 2 ]
      [ "$output" = "flush=full:unsupported" ]
      [ "$content" = OLD ]
      ;;
    *) false ;;
  esac
  left=$(count_named "$W" '.rf.*')
  [ "$left" = 0 ]
}

@test "fsync tool: flushes files and directories, fails on a missing path, -f is macOS-only" {
  build_probe
  printf 'x\n' >"$W/f"
  run_lab_c '
    "$1" "$3" "$3/f"; echo "link_rc=$?"
    "$2" fsync -- "$3/f"; echo "mode_rc=$?"
    "$1" "$3/missing" 2>/dev/null; echo "missing_rc=$?"
    "$1" 2>/dev/null; echo "usage_rc=$?"
    "$1" -f "$3" 2>/dev/null; echo "full_rc=$?"
    "$1" -d "$3/f"; echo "data_rc=$?"' sh "$FSYNC_TOOL" "$PROBE" "$W"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Darwin) full=0 ;;
    Linux) full=2 ;;
    *) false ;;
  esac
  [ "$output" = "link_rc=0
mode_rc=0
missing_rc=1
usage_rc=2
full_rc=$full
data_rc=0" ]
}

@test "bench: every flush method this system has reports a median and a maximum" {
  build_probe
  run_lab_c '"$1" bench "$2" 5' sh "$PROBE" "$W"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Darwin) want="overwrite+none overwrite+fdatasync overwrite+fsync overwrite+full overwrite+barrier replace+none replace+fsync replace+full" ;;
    Linux) want="overwrite+none overwrite+fdatasync overwrite+fsync replace+none replace+fsync" ;;
    *) false ;;
  esac
  # Every line must have this exact shape, and a median can never exceed the maximum.
  got=
  for line in "${lines[@]}"; do
    [[ $line =~ ^method=([^ ]+)\ n=5\ p50_us=([0-9]+)\ max_us=([0-9]+)$ ]] || false
    got="$got${BASH_REMATCH[1]} "
    [ "${BASH_REMATCH[2]}" -le "${BASH_REMATCH[3]}" ]
  done
  [ "$got" = "$want " ]
  left=$(count_named "$W" '.bench*')
  [ "$left" = 0 ]
}

@test "sync FILE: GNU and BusyBox open the operand (a missing one fails); macOS ignores it" {
  run_lab_c 'sync -- "$1/missing"; echo "rc=$?"' sh "$W"
  [ "$status" -eq 0 ]
  case $(sync_impl) in
    gnu)
      [ "${lines[1]}" = "rc=1" ]
      [[ ${lines[0]} == *"error opening"* ]] || false
      ;;
    busybox)
      [ "${lines[1]}" = "rc=1" ]
      [[ ${lines[0]} == *"missing"* ]] || false
      ;;
    bsd) [ "$output" = "rc=0" ] ;;
    *) false ;;
  esac
}

@test "dd conv=fsync to a pipe: fsync fails with EINVAL, so dd exits 1 in GNU, BSD, and BusyBox" {
  run_lab_c '
    printf x | { dd conv=fsync 2>"$1/err"; echo "rc=$?" >"$1/rc"; } | cat >/dev/null
    cat "$1/rc"' sh "$W"
  [ "$status" -eq 0 ]
  [ "$output" = "rc=1" ]
  case $(dd_impl) in
    gnu) grep -q "fsync failed for 'standard output': Invalid argument" "$W/err" ;;
    bsd) grep -q "fsyncing stdout: Invalid argument" "$W/err" ;;
    busybox) grep -q "Invalid argument" "$W/err" ;;
    *) false ;;
  esac
}

@test "durable_replace: new content, the requested mode, a new inode, and no leftovers" {
  build_probe
  run_lab_c '
    . "$1"
    cd "$3" || exit 9
    printf "OLD\n" >dest
    before=$(ls -i dest)
    DR_FSYNC=$2
    durable_replace 644 dest printf "NEW\n"
    rc=$?
    after=$(ls -i dest)
    if [ "$before" = "$after" ]; then same=yes; else same=no; fi
    left=$(find . -name ".dr.*" | wc -l | tr -d " ")
    echo "rc=$rc content=$(cat dest) mode=$(ls -l dest | cut -c1-10) same_inode=$same leftovers=$left"
  ' sh "$DURABLE" "$FSYNC_TOOL" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = "rc=0 content=NEW mode=-rw-r--r-- same_inode=no leftovers=0" ]
}

@test "durable_replace: order is chmod, flush file, rename, flush directory" {
  build_probe
  run_lab_c '
    . "$1"
    cd "$3" || exit 9
    printf "OLD\n" >dest
    log() {
      if [ ! -e "$1" ]; then
        echo "check-missing"
        return 1
      elif [ -d "$1" ]; then
        echo "dir dest=$(cat dest)"
      else
        echo "file mode=$(ls -l "$1" | cut -c1-10) dest=$(cat dest)"
      fi
      "$FS" "$1"
    }
    FS=$2
    DR_FSYNC=log
    durable_replace 640 dest printf "NEW\n"
    echo "rc=$?"
  ' sh "$DURABLE" "$FSYNC_TOOL" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = "check-missing
file mode=-rw-r----- dest=OLD
dir dest=NEW
rc=0" ]
}

@test "durable_replace: a failed file flush returns its status and leaves the old file" {
  build_probe
  run_lab_c '
    . "$1"
    cd "$3" || exit 9
    printf "OLD\n" >dest
    failfile() {
      [ -e "$1" ] || return 1
      if [ -d "$1" ]; then "$FS" "$1"; else return 5; fi
    }
    FS=$2
    DR_FSYNC=failfile
    durable_replace 644 dest printf "NEW\n"
    rc=$?
    left=$(find . -name ".dr.*" | wc -l | tr -d " ")
    echo "rc=$rc content=$(cat dest) leftovers=$left"
  ' sh "$DURABLE" "$FSYNC_TOOL" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = "rc=5 content=OLD leftovers=0" ]
}

@test "durable_replace: a failed directory flush returns its status after the rename" {
  build_probe
  run_lab_c '
    . "$1"
    cd "$3" || exit 9
    printf "OLD\n" >dest
    faildir() {
      [ -e "$1" ] || return 1
      if [ -d "$1" ]; then return 7; else "$FS" "$1"; fi
    }
    FS=$2
    DR_FSYNC=faildir
    durable_replace 644 dest printf "NEW\n"
    rc=$?
    left=$(find . -name ".dr.*" | wc -l | tr -d " ")
    echo "rc=$rc content=$(cat dest) leftovers=$left"
  ' sh "$DURABLE" "$FSYNC_TOOL" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = "rc=7 content=NEW leftovers=0" ]
}

@test "durable_replace: a failed producer returns its status and nothing is flushed" {
  build_probe
  run_lab_c '
    . "$1"
    cd "$3" || exit 9
    printf "OLD\n" >dest
    calls=0
    counting() {
      calls=$((calls + 1))
      [ -e "$1" ] || return 1
      "$FS" "$1"
    }
    producer() {
      printf "partial"
      return 4
    }
    FS=$2
    DR_FSYNC=counting
    durable_replace 644 dest producer
    rc=$?
    left=$(find . -name ".dr.*" | wc -l | tr -d " ")
    echo "rc=$rc content=$(cat dest) flusher_calls=$calls leftovers=$left"
  ' sh "$DURABLE" "$FSYNC_TOOL" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = "rc=4 content=OLD flusher_calls=1 leftovers=0" ]
}

@test "durable_replace with plain sync: works with GNU and BusyBox, refuses macOS sync (status 3)" {
  run_lab_c '
    . "$1"
    cd "$2" || exit 9
    printf "OLD\n" >dest
    durable_replace 644 dest printf "NEW\n"
    echo "rc=$? content=$(cat dest)"
  ' sh "$DURABLE" "$W"
  [ "$status" -eq 0 ]
  case $(sync_impl) in
    gnu | busybox) [ "$output" = "rc=0 content=NEW" ] ;;
    bsd)
      [ "${lines[0]}" = "durable_replace: 'sync' ignores its operands; set DR_FSYNC to an fsync tool" ]
      [ "${lines[1]}" = "rc=3 content=OLD" ]
      ;;
    *) false ;;
  esac
}
