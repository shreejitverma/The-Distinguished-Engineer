#!/usr/bin/env bats
# Path resolution and symlink races: what the shell's cd does with symlinks, what the kernel
# does, and why check-then-use loses. Each test builds its fixture in $BATS_TEST_TMPDIR.
# shellcheck disable=SC2016 # single-quoted code is expanded by the shell under test, not by bats

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/resolve-probe.sh"
  FIX="$BATS_TEST_TMPDIR/fix"
  mkdir "$FIX"
}

# build_rprobe: compile the C probe into the test's scratch directory; a missing compiler fails.
build_rprobe() {
  RPROBE="$BATS_TEST_TMPDIR/rprobe"
  "${CC:-cc}" -std=c99 -Wall -Wextra -Werror -o "$RPROBE" "$BATS_TEST_DIRNAME/rprobe.c"
}

# kernel_symloop: the number of symlinks one lookup may follow, from the kernel source
# (Linux include/linux/namei.h, XNU bsd/sys/param.h).
kernel_symloop() {
  case $(uname -s) in
    Linux) echo 40 ;;
    Darwin) echo 32 ;;
    *) echo unknown ;;
  esac
}

# realpath_impl: which realpath utility is installed (the shell under test does not change it).
realpath_impl() {
  local v p r kernel
  v=$(realpath --version 2>&1 </dev/null) || true
  case $v in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *)
      p=$(command -v realpath) || p=realpath
      r=$(readlink -f "$p" 2>/dev/null) || r=$p
      case $r in
        *busybox*) echo busybox ;;
        *)
          kernel=$(uname -s) || kernel=unknown
          if [ "$kernel" = Darwin ]; then echo macos; else echo unknown; fi
          ;;
      esac
      ;;
  esac
}

@test "cd and pwd are logical by default; -P resolves the link before dot-dot" {
  run_lab "$PROBE" logical "$FIX"
  [ "$status" -eq 0 ]
  [ "$output" = "pwd=/l pwd-P=/a/b cd-..=/ cd-P-l/..=/a" ]
}

@test "cd l/../x when x exists only physically: bash and zsh fall back, POSIX shells fail" {
  run_lab "$PROBE" fallback "$FIX"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      got=$output
      run_lab_c 'case :$SHELLOPTS: in *:posix:*) echo posix ;; *) echo normal ;; esac'
      case $output in
        posix) [ "$got" = FAIL ] ;;
        normal) [ "$got" = /a/x ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh) [ "$output" = /a/x ] ;;
    dash | busybox | ksh93 | mksh | posh | yash) [ "$output" = FAIL ] ;;
    *) false ;;
  esac
}

@test "a relative symlink is resolved against the link's directory, not the caller's" {
  run_lab "$PROBE" relative "$FIX"
  [ "$status" -eq 0 ]
  [ "$output" = "readlink=target cat=sub" ]
}

@test "the kernel follows at most 40 (Linux) or 32 (macOS) chained symlinks" {
  limit=$(kernel_symloop)
  [ "$limit" != unknown ]
  run_lab "$PROBE" chain "$FIX"
  [ "$status" -eq 0 ]
  [ "$output" = "followed=$limit" ]
}

@test "the limit is per lookup: one more link anywhere in the path gives ELOOP, as does O_NOFOLLOW" {
  build_rprobe
  limit=$(kernel_symloop)
  [ "$limit" != unknown ]
  run_lab "$PROBE" chain "$FIX"
  [ "$status" -eq 0 ]
  # The physical path: on macOS the scratch directory sits under /var, itself a symlink.
  phys=$(cd -P "$FIX" && pwd -P)
  ln -s . "$phys/pre"
  run_lab_c '"$0" open "$1"' "$RPROBE" "$phys/f$limit"
  [ "$output" = ok ]
  run_lab_c '"$0" open "$1"' "$RPROBE" "$phys/f$((limit + 1))"
  [ "$status" -eq 1 ]
  [ "$output" = ELOOP ]
  # pre -> . adds one link in a directory component, and the chain then needs one too many.
  run_lab_c '"$0" open "$1"' "$RPROBE" "$phys/pre/f$limit"
  [ "$status" -eq 1 ]
  [ "$output" = ELOOP ]
  run_lab_c '"$0" nofollow "$1"' "$RPROBE" "$phys/f1"
  [ "$status" -eq 1 ]
  [ "$output" = ELOOP ]
  run_lab_c '"$0" nofollow "$1"' "$RPROBE" "$phys/f0"
  [ "$output" = ok ]
  # O_NOFOLLOW governs only the last component: a link in the prefix is still followed.
  run_lab_c '"$0" nofollow "$1"' "$RPROBE" "$phys/pre/f0"
  [ "$output" = ok ]
}

@test "a trailing slash makes test -h see the directory, not the link" {
  run_lab "$PROBE" slash "$FIX"
  [ "$status" -eq 0 ]
  [ "$output" = "h(l)=y h(l/)=n d(l/)=y h(dang)=y e(dang)=n" ]
}

@test "redirecting into a dangling symlink creates the file it names" {
  run_lab "$PROBE" dangling "$FIX"
  [ "$status" -eq 0 ]
  [ "$output" = "victim=data dang-still-link=y" ]
  [ -f "$FIX/victim" ]
}

@test "check-then-write follows a link planted in the window; rename replaces the link" {
  run_lab "$PROBE" toctou "$FIX"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "unsafe: protected=clobbered" ]
  [ "${lines[1]}" = "rename: protected=original out-is-link=n out=fresh" ]
}

@test "mv onto a symlink to a directory moves the file into the directory" {
  run_lab "$PROBE" mvdir "$FIX"
  [ "$status" -eq 0 ]
  [ "$output" = "dl-is-link=y realdir=newfile" ]
}

@test "a directory descriptor survives a rename: /dev/fd/N/file works on Linux, not macOS" {
  run_lab "$PROBE" pin "$FIX"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Linux) [ "$output" = "old-exists=n via-fd=pinned" ] ;;
    Darwin) [ "$output" = "old-exists=n via-fd=FAIL" ] ;;
    *) false ;;
  esac
}

@test "walking with openat and O_NOFOLLOW refuses a symlink in any component, and dot-dot" {
  build_rprobe
  mkdir -p "$FIX/root/d" "$FIX/outside"
  printf 'inside\n' >"$FIX/root/d/f"
  printf 'secret\n' >"$FIX/outside/f"
  ln -s ../outside "$FIX/root/esc"
  ln -s d/f "$FIX/root/lf"
  run_lab_c '"$0" walk "$1" "$2"' "$RPROBE" "$FIX/root" d/f
  [ "$status" -eq 0 ]
  [ "$output" = inside ]
  run_lab_c '"$0" walk "$1" "$2"' "$RPROBE" "$FIX/root" esc/f
  [ "$status" -eq 1 ]
  [ "$output" = "refused esc: symbolic link" ]
  run_lab_c '"$0" walk "$1" "$2"' "$RPROBE" "$FIX/root" lf
  [ "$status" -eq 1 ]
  [ "$output" = "refused lf: symbolic link" ]
  run_lab_c '"$0" walk "$1" "$2"' "$RPROBE" "$FIX/root" d/../../outside/f
  [ "$status" -eq 1 ]
  [ "$output" = "refused ..: dot-dot" ]
  # The same path through the kernel's ordinary resolution escapes.
  run_lab_c 'cat "$1"' sh "$FIX/root/esc/f"
  [ "$output" = secret ]
}

@test "realpath on a dangling relative link: GNU resolves it, BusyBox misplaces it, macOS fails" {
  mkdir "$FIX/sub"
  ln -s nofile "$FIX/sub/link"
  phys=$(cd -P "$FIX" && pwd -P)
  run_lab_c 'realpath "$1"' sh "$FIX/sub/link"
  case $(realpath_impl) in
    gnu | uutils)
      [ "$status" -eq 0 ]
      [ "$output" = "$phys/sub/nofile" ]
      ;;
    busybox)
      # BusyBox joins a relative link target to the caller's cwd (libbb/xreadlink.c).
      [ "$status" -eq 0 ]
      here=$(pwd -P)
      [ "$output" = "$here/nofile" ]
      ;;
    macos)
      [ "$status" -eq 1 ]
      [ -z "${output##*No such file or directory*}" ]
      ;;
    *) false ;;
  esac
}
