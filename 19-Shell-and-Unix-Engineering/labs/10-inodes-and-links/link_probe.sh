#!/bin/sh
set -eu

# link_probe.sh - test harness for inode and link behaviors
# Implements discriminating probes for hard links, symlinks, and directory semantics.

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage: link_probe.sh <command> [args...]

commands:
  hardlink <dir>       verify hard link count and inode sharing
  symlink <dir>        verify symlink classification and dangling link checks
  atomic_swap <dir>    verify atomic symlink replacement pointing to directories
  trailing_slash <dir> verify trailing slash resolution semantics
EOF
  exit 1
}

cmd_hardlink() {
  dir="${1:-}"
  [ -n "$dir" ] || die "hardlink requires directory"
  [ -d "$dir" ] || die "directory does not exist: $dir"

  base="$dir/base.txt"
  link1="$dir/link1.txt"
  link2="$dir/link2.txt"

  printf 'initial payload\n' >"$base"
  ln "$base" "$link1"
  ln "$base" "$link2"

  if [ "$base" -ef "$link1" ] && [ "$link1" -ef "$link2" ]; then
    printf 'ef_hardlink=true\n'
  else
    printf 'ef_hardlink=false\n'
  fi

  # Modify content through link2, read through base
  printf 'updated via link2\n' >"$link2"
  content=$(cat "$base")
  printf 'read_via_base=%s\n' "$content"

  # Remove base and link1, verify link2 survives with full content
  rm -f "$base" "$link1"
  if [ -f "$link2" ]; then
    surviving=$(cat "$link2")
    printf 'surviving_content=%s\n' "$surviving"
  else
    printf 'surviving_content=missing\n'
  fi
}

cmd_symlink() {
  dir="${1:-}"
  [ -n "$dir" ] || die "symlink requires directory"
  [ -d "$dir" ] || die "directory does not exist: $dir"

  target="$dir/target.txt"
  symlink="$dir/symlink.txt"
  broken="$dir/broken.txt"

  printf 'target payload\n' >"$target"
  ln -s target.txt "$symlink"
  ln -s nonexistent.txt "$broken"

  # Classification tests
  [ -h "$symlink" ] && h_sym="yes" || h_sym="no"
  [ -L "$symlink" ] && l_sym="yes" || l_sym="no"
  [ -e "$symlink" ] && e_sym="yes" || e_sym="no"

  [ -h "$broken" ] && h_brk="yes" || h_brk="no"
  [ -L "$broken" ] && l_brk="yes" || l_brk="no"
  [ -e "$broken" ] && e_brk="yes" || e_brk="no"

  # -ef follows symlinks to existing target
  [ "$symlink" -ef "$target" ] && ef_valid="yes" || ef_valid="no"
  [ "$broken" -ef "$broken" ] && ef_broken="yes" || ef_broken="no"

  printf 'valid_symlink: -h=%s -L=%s -e=%s -ef_target=%s\n' "$h_sym" "$l_sym" "$e_sym" "$ef_valid"
  printf 'broken_symlink: -h=%s -L=%s -e=%s -ef_self=%s\n' "$h_brk" "$l_brk" "$e_brk" "$ef_broken"
}

cmd_atomic_swap() {
  dir="${1:-}"
  [ -n "$dir" ] || die "atomic_swap requires directory"
  [ -d "$dir" ] || die "directory does not exist: $dir"

  v1="$dir/release_v1"
  v2="$dir/release_v2"
  mkdir "$v1" "$v2"
  printf 'version 1\n' >"$v1/version.txt"
  printf 'version 2\n' >"$v2/version.txt"

  # Initial pointer
  ln -s release_v1 "$dir/current"
  initial=$(cat "$dir/current/version.txt")
  printf 'initial_version=%s\n' "$initial"

  # Create staging symlink
  ln -s release_v2 "$dir/current.tmp"

  # Atomically replace current. Detect whether mv accepts -T (GNU) or -h (macOS/BSD)
  if mv -T "$dir/current.tmp" "$dir/current" 2>/dev/null; then
    swap_mode="gnu_T"
  elif mv -h "$dir/current.tmp" "$dir/current" 2>/dev/null; then
    swap_mode="bsd_h"
  else
    # Fallback: rename(2) via small C helper if probe binary exists, or unlink+link
    die "neither mv -T nor mv -h supported on this host"
  fi

  swapped=$(cat "$dir/current/version.txt")
  printf 'swap_mode=%s\n' "$swap_mode"
  printf 'swapped_version=%s\n' "$swapped"
}

cmd_trailing_slash() {
  dir="${1:-}"
  [ -n "$dir" ] || die "trailing_slash requires directory"
  [ -d "$dir" ] || die "directory does not exist: $dir"

  regular="$dir/file.txt"
  sub_dir="$dir/subdir"
  printf 'hello\n' >"$regular"
  mkdir "$sub_dir"

  link_file="$dir/link_file"
  link_dir="$dir/link_dir"
  ln -s file.txt "$link_file"
  ln -s subdir "$link_dir"

  # Trailing slash on symlink to regular file must fail with ENOTDIR
  if cat "$link_file/" >/dev/null 2>&1; then
    file_slash="succeeded"
  else
    file_slash="enotdir"
  fi

  # Trailing slash on symlink to directory must succeed
  if [ -d "$link_dir/" ]; then
    dir_slash="directory"
  else
    dir_slash="failed"
  fi

  printf 'file_slash=%s\n' "$file_slash"
  printf 'dir_slash=%s\n' "$dir_slash"
}

[ $# -ge 1 ] || usage

cmd="$1"
shift
case "$cmd" in
  hardlink) cmd_hardlink "$@" ;;
  symlink) cmd_symlink "$@" ;;
  atomic_swap) cmd_atomic_swap "$@" ;;
  trailing_slash) cmd_trailing_slash "$@" ;;
  *) usage ;;
esac
