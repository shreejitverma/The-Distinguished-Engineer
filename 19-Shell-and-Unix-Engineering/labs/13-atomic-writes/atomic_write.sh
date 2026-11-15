#!/bin/sh
# atomic_write.sh - replace a file so that every reader sees the complete old or the
# complete new content, never a mix, and a failed producer leaves the old file alone.
#
# Source it, then call:  atomic_write [-m MODE] DEST CMD [ARG...]
# CMD runs with standard output on a temporary file in DEST's own directory; only if
# CMD exits 0 does the file get its mode and replace DEST with one rename(2).
# The status is CMD's status when CMD fails, 2 for a usage or setup error, and the
# status of chmod, dd, or mv when one of those fails.
# Set AW_SYNC=1 to fsync the data before the rename and the directory after it.
# A caller that traps signals should remove "$aw_tmp" in its handler.
# POSIX sh plus mktemp(1), which every userland ships but POSIX does not specify.

# aw_triple RWX: set aw_p to the chmod letters for one ls permission triple ("r-s" -> rx).
aw_triple() {
  aw_p=
  case $1 in r??) aw_p=r ;; *) ;; esac
  case $1 in ?w?) aw_p=${aw_p}w ;; *) ;; esac
  case $1 in ??[xst]) aw_p=${aw_p}x ;; *) ;; esac
}

# aw_mode_of FILE: set aw_mode to FILE's rwx bits as a symbolic mode, from `ls -ld`.
# set-user-ID, set-group-ID, and sticky bits are deliberately not carried over.
aw_mode_of() {
  aw_ls=$(ls -ld -- "$1") || return 1
  aw_bits=${aw_ls%% *}         # "-rw-r-----" plus an optional "@", "+" or "."
  aw_bits=${aw_bits#?}         # drop the file type character
  aw_rest=${aw_bits#?????????} # whatever follows the nine permission characters
  aw_bits=${aw_bits%"$aw_rest"}
  aw_triple "${aw_bits%??????}"
  aw_mode=u=$aw_p
  aw_g=${aw_bits#???}
  aw_triple "${aw_g%???}"
  aw_mode=$aw_mode,g=$aw_p
  aw_triple "${aw_bits#??????}"
  aw_mode=$aw_mode,o=$aw_p
}

# aw_flush FILE: fsync FILE through dd, which opens it without truncating and copies
# nothing; conv=fsync is an extension in GNU, BSD and macOS, and BusyBox dd.
aw_flush() {
  dd if=/dev/null of="$1" conv=notrunc,fsync 2>/dev/null
}

atomic_write() {
  aw_mode=
  if [ "${1-}" = -m ]; then
    [ "$#" -ge 2 ] || return 2
    aw_mode=$2
    shift 2
  fi
  if [ "$#" -lt 2 ]; then
    echo "usage: atomic_write [-m MODE] DEST CMD [ARG...]" >&2
    return 2
  fi
  aw_dest=$1
  shift
  case $aw_dest in
    */)
      echo "atomic_write: $aw_dest: trailing slash" >&2
      return 2
      ;;
    */*) aw_dir=${aw_dest%/*} ;;
    *) aw_dir=. ;;
  esac
  aw_dir=${aw_dir:-/} # "/name" has an empty directory part
  if [ -L "$aw_dest" ]; then
    echo "atomic_write: $aw_dest is a symlink; pass the file it points to" >&2
    return 2
  fi
  if [ -e "$aw_dest" ] && [ ! -f "$aw_dest" ]; then
    echo "atomic_write: $aw_dest is not a regular file" >&2
    return 2
  fi
  if [ -z "$aw_mode" ]; then
    if [ -e "$aw_dest" ]; then
      aw_mode_of "$aw_dest" || return 2
    else
      aw_umask=$(umask -S) || return 2
      aw_mode=$aw_umask,a-x # what a plain "> DEST" would have created
    fi
  fi
  # A short fixed template: ".aw." keeps the name out of "*" globs and adds only
  # 10 bytes, so it never overflows NAME_MAX the way ".$basename.XXXXXX" can.
  aw_tmp=$(mktemp "$aw_dir/.aw.XXXXXX") || return 2
  if "$@" >"$aw_tmp"; then
    :
  else
    aw_rc=$?
    rm -f -- "$aw_tmp"
    return "$aw_rc"
  fi
  if [ "${AW_SYNC-}" = 1 ]; then
    aw_flush "$aw_tmp" || {
      aw_rc=$?
      rm -f -- "$aw_tmp"
      return "$aw_rc"
    }
  fi
  chmod -- "$aw_mode" "$aw_tmp" || {
    aw_rc=$?
    rm -f -- "$aw_tmp"
    return "$aw_rc"
  }
  mv -f -- "$aw_tmp" "$aw_dest" || {
    aw_rc=$?
    rm -f -- "$aw_tmp"
    return "$aw_rc"
  }
  if [ "${AW_SYNC-}" = 1 ]; then
    # GNU and BusyBox sync fsync a named directory; macOS sync ignores operands and
    # calls sync(2), which "may return before the buffers are completely flushed", so
    # this step is best effort there. DEST is already replaced here, so a failure
    # means "visible but maybe not durable", not "old content".
    sync -- "$aw_dir" || return
  fi
  return 0
}

# atomic_create DEST CMD [ARG...]: publish DEST once; fail if DEST already exists.
# link(2) refuses an existing name with EEXIST, so the check and the publish are one
# step, and the name never points at an incomplete file.
atomic_create() {
  ac_dest=$1
  shift
  case $ac_dest in
    */*) ac_dir=${ac_dest%/*} ;;
    *) ac_dir=. ;;
  esac
  ac_dir=${ac_dir:-/}
  ac_umask=$(umask -S) || return 2
  ac_tmp=$(mktemp "$ac_dir/.aw.XXXXXX") || return 2
  if "$@" >"$ac_tmp" && chmod -- "$ac_umask,a-x" "$ac_tmp" && ln -- "$ac_tmp" "$ac_dest" 2>/dev/null; then
    rm -f -- "$ac_tmp"
    return 0
  fi
  rm -f -- "$ac_tmp"
  return 1
}
