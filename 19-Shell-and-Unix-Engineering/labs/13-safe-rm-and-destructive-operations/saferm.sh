#!/bin/sh
# saferm.sh: remove each TARGET only if it names an entry strictly inside ROOT.
# Usage: saferm.sh [-n] ROOT TARGET...
#   -n  dry run: print what would be removed and remove nothing.
# Each TARGET is checked on its own: it must be non-empty, must not be / or end in . or ..,
# and its parent directory, with every symbolic link resolved, must be ROOT or lie under ROOT.
# The final component is never followed, so a symbolic link operand removes only the link.
# Exit status: 0 every target removed (or already absent), 1 a target was refused or
# could not be removed, 2 usage error or unusable ROOT.
set -u
unset CDPATH

usage() {
  printf 'usage: saferm.sh [-n] ROOT TARGET...\n' >&2
  exit 2
}

refuse() {
  printf 'saferm: refusing %s: %s\n' "$1" "$2" >&2
  return 1
}

# physdir DIR: set REPLY to the physical absolute path of directory DIR.
# The sentinel x keeps a trailing newline in a directory name from being stripped
# by the command substitution; ${p%?x} then removes the newline pwd added and the x.
physdir() {
  REPLY=$(cd -P -- "$1" && pwd -P && echo x) || return 1
  REPLY=${REPLY%?x}
}

# remove_one TARGET: validate TARGET against $root and remove it (or report it under -n).
remove_one() {
  t=$1
  [ -n "$t" ] || {
    refuse "''" 'empty operand'
    return
  }
  # Strip trailing slashes: "link/" would make rm follow the link into its target.
  while :; do
    case $t in
      */) t=${t%/} ;;
      *) break ;;
    esac
  done
  [ -n "$t" ] || {
    refuse "$1" 'resolves to /'
    return
  }
  base=${t##*/}
  case $base in
    . | ..)
      refuse "$1" 'ends in . or ..'
      return
      ;;
    *) ;;
  esac
  case $t in
    */*)
      parent=${t%/*}
      [ -n "$parent" ] || parent=/
      ;;
    *) parent=. ;;
  esac
  physdir "$parent" || {
    refuse "$1" 'parent directory is not accessible'
    return
  }
  dir=$REPLY
  case $dir in
    "$root" | "$root"/*) ;;
    *)
      refuse "$1" "outside $root"
      return
      ;;
  esac
  victim=$dir/$base
  if [ ! -e "$victim" ] && [ ! -h "$victim" ]; then
    printf 'saferm: absent: %s\n' "$victim"
    return 0
  fi
  if [ "$dry" -eq 1 ]; then
    printf 'would remove: %s\n' "$victim"
    return 0
  fi
  # Pin the parent as the working directory, re-check where it really is, and remove
  # the entry relative to it, so a parent swapped after the check above is caught.
  (
    cd -P -- "$dir" || exit 1
    physdir . || exit 1
    [ "$REPLY" = "$dir" ] || exit 1
    rm -rf -- "./$base"
  ) || {
    refuse "$1" 'rm failed or the parent moved'
    return
  }
  if [ -e "$victim" ] || [ -h "$victim" ]; then
    refuse "$1" 'still present after rm'
    return
  fi
  printf 'removed: %s\n' "$victim"
}

dry=0
if [ "${1-}" = -n ]; then
  dry=1
  shift
fi
[ "$#" -ge 2 ] || usage
[ -n "$1" ] || {
  printf 'saferm: ROOT is empty\n' >&2
  exit 2
}
physdir "$1" || {
  printf 'saferm: ROOT is not an accessible directory: %s\n' "$1" >&2
  exit 2
}
root=$REPLY
[ "$root" != / ] || {
  printf 'saferm: ROOT must not be /\n' >&2
  exit 2
}
shift

rc=0
for target in "$@"; do
  remove_one "$target" || rc=1
done
exit "$rc"
