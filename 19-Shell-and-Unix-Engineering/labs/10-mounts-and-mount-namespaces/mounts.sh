#!/bin/sh
# mounts.sh - answer "which mount serves this path?" from a script, portably.
# Part of 19-Shell-and-Unix-Engineering/10-Files-Filesystems-and-Permissions/Mounts-and-Mount-Namespaces
#
# Usage:
#   mounts.sh decode FIELD           decode the octal escapes the kernel writes in mountinfo
#   mounts.sh lookup PATH FILE       top-most mount in a mountinfo-format FILE that serves PATH
#   mounts.sh live PATH              lookup PATH in /proc/self/mountinfo (Linux only)
#   mounts.sh dfroot PATH            the "Mounted on" column of POSIX `df -P PATH`
#
# PATH must be absolute and canonical (no symlinks, no "." or ".." components):
# mountinfo records mount points as the kernel resolved them, relative to the process root.
#
# parse_line reports a malformed record through its status, which callers test with ||;
# it runs no command whose failure set -e should catch.
# shellcheck disable=SC2310 # see above
set -eu

# decode FIELD: the kernel writes space, tab, newline and backslash in a mount point as a
# backslash and three octal digits (seq_mangle_path() in fs/seq_file.c), for example \040.
# Rewrite each \ooo as \0ooo, the form printf %b understands, and keep trailing newlines
# alive through command substitution with a sentinel character.
decode() {
  d_esc=$(printf '%s\n' "$1" | sed 's/\\\([0-7][0-7][0-7]\)/\\0\1/g')
  d_out=$(printf '%bx' "$d_esc")
  d_out=${d_out%x}
}

# parse_line LINE: split one mountinfo record into f_id, f_parent, f_point (still escaped),
# f_type, and f_opt (the optional fields, empty for a private mount). Fields are separated by
# single spaces and never contain a raw space, so parameter expansion splits them exactly,
# in every shell and without depending on IFS. Fails on a malformed record.
parse_line() {
  p_rest=$1
  f_id=${p_rest%% *}
  p_rest=${p_rest#* }
  f_parent=${p_rest%% *}
  p_rest=${p_rest#* }
  p_rest=${p_rest#* } # (3) major:minor
  p_rest=${p_rest#* } # (4) root of the mount inside its filesystem
  f_point=${p_rest%% *}
  p_rest=${p_rest#* }
  p_rest=${p_rest#* } # (6) per-mount options
  case $p_rest in
    "- "*)
      f_opt=
      p_rest=${p_rest#- }
      ;;
    *" - "*)
      f_opt=${p_rest%% - *}
      p_rest=${p_rest#* - }
      ;;
    *)
      return 1
      ;;
  esac
  f_type=${p_rest%% *}
  case $f_id$f_parent in
    "" | *[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

# lookup PATH FILE: the longest mount point that is PATH or a directory prefix of it wins;
# among mounts stacked on that same point, the top-most is the one that is no other
# stacked mount's parent (proc_pid_mountinfo(5), field 2), so follow the parent chain upward.
lookup() {
  l_path=$1
  l_file=$2
  l_best=
  l_len=-1
  while IFS= read -r l_line || [ -n "$l_line" ]; do
    parse_line "$l_line" || continue
    decode "$f_point"
    case $d_out in
      /) l_hit=yes ;;
      *)
        case $l_path in
          "$d_out" | "$d_out"/*) l_hit=yes ;;
          *) l_hit=no ;;
        esac
        ;;
    esac
    [ "$l_hit" = yes ] || continue
    if [ "${#d_out}" -gt "$l_len" ]; then
      l_len=${#d_out}
      l_best=$f_id
      l_point=$f_point
    fi
  done <"$l_file"
  if [ -z "$l_best" ]; then
    printf 'no mount serves %s\n' "$l_path" >&2
    return 1
  fi
  # Climb the stack at l_point: replace l_best by a mount at the same point whose parent it is.
  l_moved=yes
  while [ "$l_moved" = yes ]; do
    l_moved=no
    while IFS= read -r l_line || [ -n "$l_line" ]; do
      parse_line "$l_line" || continue
      if [ "$f_point" = "$l_point" ] && [ "$f_parent" = "$l_best" ] && [ "$f_id" != "$l_best" ]; then
        l_best=$f_id
        l_moved=yes
      fi
    done <"$l_file"
  done
  while IFS= read -r l_line || [ -n "$l_line" ]; do
    parse_line "$l_line" || continue
    [ "$f_id" = "$l_best" ] || continue
    decode "$f_point"
    printf 'id=%s point=<%s> fstype=%s propagation=%s\n' \
      "$f_id" "$d_out" "$f_type" "${f_opt:-private}"
    return 0
  done <"$l_file"
  return 1
}

mode=${1-}
case $mode in
  decode)
    decode "$2"
    printf '<%s>\n' "$d_out"
    ;;
  lookup)
    lookup "$2" "$3"
    ;;
  live)
    if [ -r /proc/self/mountinfo ]; then
      lookup "$2" /proc/self/mountinfo
    else
      printf 'mountinfo: unavailable\n'
    fi
    ;;
  dfroot)
    # POSIX df -P: "%s %d %d %d %d%% %s\n"; the mount point follows the first "%" and
    # its padding (assumes the file system name contains no "%"). The mount point may
    # contain spaces, so take the rest of the line rather than a whitespace field.
    df_out=$(df -P "$2")
    printf '%s\n' "$df_out" | sed -n '2s/^[^%]*%  *//p'
    ;;
  *)
    printf 'usage: %s decode FIELD | lookup PATH FILE | live PATH | dfroot PATH\n' "$0" >&2
    exit 2
    ;;
esac
