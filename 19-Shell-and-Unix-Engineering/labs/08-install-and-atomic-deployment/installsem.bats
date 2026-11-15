#!/usr/bin/env bats
# install(1) and atomic deployment: how GNU, BSD and macOS, BusyBox, and uutils install
# replace a destination, which options mean different things on each, and two
# deployment helpers that rely only on rename(2) for atomicity.
# The shell under test runs the probes; the install, cp, mv, and ln on PATH decide the
# behavior, so the tests branch on the utility implementation, not on the shell family.
# shellcheck disable=SC2312,SC2012 # each $(...) is compared against an exact value, so a failed read fails the test; the ls listings name plain ASCII files the test created

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/installsem.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

# util_flavor CMD: print gnu, busybox, uutils, or bsd for the utility on PATH, or unknown.
util_flavor() {
  local out
  out=$("$1" --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"usage: $1"*) echo bsd ;; # BSD and macOS reject --version and print their usage
    *) echo unknown ;;
  esac
}

# build_c NAME SOURCE [CFLAG...]: compile SOURCE into the test directory as NAME and print its path.
build_c() {
  local name=$1 source=$2
  shift 2
  "${CC:-cc}" -Wall -Wextra -Werror "$@" -o "$BATS_TEST_TMPDIR/$name" \
    "$BATS_TEST_DIRNAME/$source"
  printf '%s\n' "$BATS_TEST_TMPDIR/$name"
}

@test "install replaces the destination with a new inode: an open reader and a hard link keep the old bytes" {
  run_lab "$PROBE" replace
  [ "$status" -eq 0 ]
  case $(util_flavor install) in
    gnu | bsd | busybox | uutils)
      [ "$output" = "rc=0 fd=old link=old name=new same_inode=no mode=-rwxr-xr-x" ]
      ;;
    *) false ;;
  esac
}

@test "the installed mode ignores the umask: 0755 by default, exactly -m otherwise" {
  run_lab "$PROBE" umask
  [ "$status" -eq 0 ]
  case $(util_flavor install) in
    gnu | bsd | busybox | uutils) [ "$output" = "default=-rwxr-xr-x m600=-rw-------" ] ;;
    *) false ;;
  esac
}

@test "install replaces a symlink at the destination instead of writing through it" {
  run_lab "$PROBE" symlink-dest
  [ "$status" -eq 0 ]
  case $(util_flavor install) in
    gnu | bsd | busybox | uutils) [ "$output" = "rc=0 dst_is_link=no victim=victim dst=new" ] ;;
    *) false ;;
  esac
}

@test "install -C leaves an identical destination alone and replaces a changed one; BusyBox has no -C" {
  run_lab "$PROBE" compare
  [ "$status" -eq 0 ]
  case $(util_flavor install) in
    gnu | bsd | uutils) [ "$output" = "rc=0 identical_kept=yes changed_replaced=yes content=changed" ] ;;
    busybox) [ "$output" = "rc=1" ] ;;
    *) false ;;
  esac
}

@test "install -D SRC a/b/c creates the parents on GNU, BusyBox, and uutils but is a usage error on BSD" {
  run_lab "$PROBE" mkparents
  [ "$status" -eq 0 ]
  case $(util_flavor install) in
    gnu | busybox | uutils) [ "$output" = "rc=0 created=yes" ] ;;
    bsd) [ "$output" = "rc=64 created=no" ] ;; # -D takes "src" as DESTDIR, leaving one operand
    *) false ;;
  esac
}

@test "install -b keeps the old file as dst~ (GNU, uutils), dst.old (BSD), or not at all (BusyBox)" {
  run_lab "$PROBE" backup
  [ "$status" -eq 0 ]
  case $(util_flavor install) in
    gnu | uutils) [ "$output" = "rc=0 dst=new backup=dst~ content=old" ] ;;
    bsd) [ "$output" = "rc=0 dst=new backup=dst.old content=old" ] ;;
    busybox) [ "$output" = "rc=0 dst=new backup=none" ] ;;
    *) false ;;
  esac
}

@test "install -d -m 700 gives the leaf directory exactly mode 700" {
  run_lab "$PROBE" dirmode
  [ "$status" -eq 0 ]
  case $(util_flavor install) in
    gnu | bsd | busybox | uutils) [ "$output" = "rc=0 leaf=drwx------" ] ;;
    *) false ;;
  esac
}

@test "install replaces a running executable; the old process finishes on the old inode" {
  old=$(build_c oldapp exitprog.c -DCODE=7)
  new=$(build_c newapp exitprog.c -DCODE=9)
  run_lab "$PROBE" running "$old" "$new" install
  [ "$status" -eq 0 ]
  case $(util_flavor install) in
    gnu | bsd | busybox | uutils) [ "$output" = "tool_rc=0 old=7 now=9" ] ;;
    *) false ;;
  esac
}

@test "cp onto a running executable fails with ETXTBSY on Linux unless cp replaces the inode" {
  old=$(build_c oldapp exitprog.c -DCODE=7)
  new=$(build_c newapp exitprog.c -DCODE=9)
  run_lab "$PROBE" running "$old" "$new" cp
  [ "$status" -eq 0 ]
  case $(uname -s)/$(util_flavor cp) in
    Linux/gnu) [ "$output" = "tool_rc=1 old=7 now=7" ] ;;     # open(O_WRONLY|O_TRUNC) -> ETXTBSY
    Linux/busybox) [ "$output" = "tool_rc=0 old=7 now=9" ] ;; # O_EXCL fails, unlink, create
    Darwin/bsd) [ "$output" = "tool_rc=0 old=7 now=9" ] ;;    # XNU has no ETXTBSY for this; rewrites in place
    *) false ;;
  esac
}

@test "cp truncates a mapped file under the mapper (SIGBUS); install leaves the mapping intact" {
  probe=$(build_c mapprobe mapprobe.c)
  run_lab "$PROBE" mmap "$probe" install
  [ "$status" -eq 0 ]
  case $(util_flavor install) in
    gnu | bsd | busybox | uutils) [ "$output" = "first=A last=A" ] ;;
    *) false ;;
  esac
  run_lab "$PROBE" mmap "$probe" cp
  [ "$status" -eq 0 ]
  case $(util_flavor cp) in
    gnu | bsd) [ "$output" = "first=B last=SIGBUS" ] ;;
    busybox) [ "$output" = "first=A last=A" ] ;; # FEATURE_NON_POSIX_CP: unlink and create
    *) false ;;
  esac
}

@test "plain mv onto a current symlink moves the new link INTO the old release" {
  run_lab "$PROBE" mv-into
  [ "$status" -eq 0 ]
  [ "$output" = "rc=0 current=releases/r1 nested=next" ]
}

@test "ln -sf follows a symlink to a directory and nests the link; ln -sfn replaces it" {
  run_lab "$PROBE" ln-nest
  [ "$status" -eq 0 ]
  [ "$output" = "current=releases/r1 nested=r2 then_n=releases/r2" ]
}

@test "atomic-install.sh replaces DEST with a new inode of the given mode and leaves no staging files" {
  dir=$(mktemp -d "$BATS_TEST_TMPDIR/ai.XXXXXX")
  printf 'old\n' >"$dir/app.conf"
  ln "$dir/app.conf" "$dir/alias"
  printf 'new\n' >"$dir/src"
  run_lab "$BATS_TEST_DIRNAME/atomic-install.sh" 640 "$dir/src" "$dir/app.conf"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
  [ "$(cat "$dir/app.conf")" = new ]
  [ "$(cat "$dir/alias")" = old ]
  [ "$(ls -l "$dir/app.conf" | cut -c1-10)" = "-rw-r-----" ]
  [ "$(cd "$dir" && ls -A | LC_ALL=C sort | tr '\n' ' ')" = "alias app.conf src " ]
}

@test "atomic-install.sh refuses a directory DEST and cleans up after a failed install" {
  dir=$(mktemp -d "$BATS_TEST_TMPDIR/ai.XXXXXX")
  mkdir "$dir/target"
  printf 'new\n' >"$dir/src"
  run_lab "$BATS_TEST_DIRNAME/atomic-install.sh" 644 "$dir/src" "$dir/target"
  [ "$status" -eq 1 ]
  [ "$output" = "atomic-install.sh: $dir/target: is a directory" ]
  [ -z "$(ls -A "$dir/target")" ]
  printf 'keep\n' >"$dir/app.conf"
  run_lab "$BATS_TEST_DIRNAME/atomic-install.sh" 644 "$dir/missing" "$dir/app.conf"
  [ "$status" -ne 0 ]
  [ "$(cat "$dir/app.conf")" = keep ]
  [ "$(cd "$dir" && ls -A | LC_ALL=C sort | tr '\n' ' ')" = "app.conf src target " ]
}

@test "swap-release.sh creates, swaps, and rolls back current without nesting or leftovers" {
  base=$(mktemp -d "$BATS_TEST_TMPDIR/sr.XXXXXX")
  mkdir -p "$base/releases/r1" "$base/releases/r2"
  printf 'one\n' >"$base/releases/r1/VERSION"
  printf 'two\n' >"$base/releases/r2/VERSION"
  run_lab "$BATS_TEST_DIRNAME/swap-release.sh" "$base" r1
  [ "$status" -eq 0 ]
  [ "$(readlink "$base/current")" = releases/r1 ]
  [ "$(cat "$base/current/VERSION")" = one ]
  run_lab "$BATS_TEST_DIRNAME/swap-release.sh" "$base" r2
  [ "$status" -eq 0 ]
  [ "$(cat "$base/current/VERSION")" = two ]
  [ "$(ls "$base/releases/r1")" = VERSION ]
  run_lab "$BATS_TEST_DIRNAME/swap-release.sh" "$base" r1
  [ "$status" -eq 0 ]
  [ "$(cat "$base/current/VERSION")" = one ]
  [ "$(ls "$base/releases/r2")" = VERSION ]
  [ "$(cd "$base" && ls -A | LC_ALL=C sort | tr '\n' ' ')" = "current releases " ]
}

@test "swap-release.sh rejects a missing release, a path as a name, and a real directory at current" {
  base=$(mktemp -d "$BATS_TEST_TMPDIR/sr.XXXXXX")
  mkdir -p "$base/releases/r1"
  run_lab "$BATS_TEST_DIRNAME/swap-release.sh" "$base" r9
  [ "$status" -eq 1 ]
  [ "$output" = "swap-release.sh: $base/releases/r9: no such release" ]
  run_lab "$BATS_TEST_DIRNAME/swap-release.sh" "$base" ../r1
  [ "$status" -eq 1 ]
  [ "$output" = "swap-release.sh: ../r1: not a release name" ]
  mkdir "$base/current"
  run_lab "$BATS_TEST_DIRNAME/swap-release.sh" "$base" r1
  [ "$status" -eq 1 ]
  [ "$output" = "swap-release.sh: $base/current: exists and is not a symlink" ]
  [ "$(cd "$base" && ls -A | LC_ALL=C sort | tr '\n' ' ')" = "current releases " ]
}
