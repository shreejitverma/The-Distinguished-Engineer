# shellcheck shell=sh
# durable.sh - durable_replace: replace a file so that readers see the old or the new
# content, and so that after a crash the name holds the old or the new content too.
# Source it: . ./durable.sh
#
#   durable_replace MODE DEST CMD [ARG...]
#
# Runs CMD with standard output on a temporary file in DEST's directory, then:
#   1. chmod MODE on the temporary file
#   2. flush the temporary file (data, size, and mode) to storage
#   3. rename it over DEST (one atomic directory update)
#   4. flush DEST's directory, so the new directory entry is on storage
# Returns CMD's status if CMD fails, the flusher's status if a flush fails, 2 for usage
# or setup errors, and 3 when no flusher that honors its operands is available.
# DEST is untouched unless step 3 ran; a failure in step 4 means "new content visible,
# not yet known to be durable".
#
# The flusher is "$DR_FSYNC PATH" when DR_FSYNC is set (FreeBSD and BusyBox ship an
# fsync utility; the lab builds one), else "sync -- PATH", which calls fsync(2) on PATH
# in GNU coreutils 8.24+ and BusyBox built with FEATURE_SYNC_FANCY. macOS sync ignores
# its operands and calls sync(2), so durable_replace refuses to run with it (status 3).
# Variables use a dr_ prefix because POSIX sh has no "local".

dr_flush() {
  if [ -n "${DR_FSYNC-}" ]; then
    "$DR_FSYNC" "$1"
  else
    sync -- "$1"
  fi
}

# dr_check_flusher DIR: a flusher that really opens its operand must fail on a missing
# path; one that ignores operands (macOS sync) succeeds, and is useless here.
dr_check_flusher() {
  [ "${dr_flusher_ok-}" = 1 ] && return 0
  if dr_flush "$1/.dr-missing.$$" 2>/dev/null; then
    echo "durable_replace: '${DR_FSYNC:-sync}' ignores its operands; set DR_FSYNC to an fsync tool" >&2
    return 3
  fi
  dr_flusher_ok=1
}

durable_replace() {
  if [ "$#" -lt 3 ]; then
    echo "usage: durable_replace MODE DEST CMD [ARG...]" >&2
    return 2
  fi
  dr_mode=$1
  dr_dest=$2
  shift 2
  case $dr_dest in
    */)
      echo "durable_replace: $dr_dest: trailing slash" >&2
      return 2
      ;;
    */*) dr_dir=${dr_dest%/*} ;;
    *) dr_dir=. ;;
  esac
  dr_dir=${dr_dir:-/}
  dr_check_flusher "$dr_dir" || return
  dr_tmp=$(mktemp "$dr_dir/.dr.XXXXXX") || return 2
  if "$@" >"$dr_tmp"; then
    :
  else
    dr_rc=$?
    rm -f -- "$dr_tmp"
    return "$dr_rc"
  fi
  # chmod before the flush, so the flush also covers the new mode.
  chmod -- "$dr_mode" "$dr_tmp" || {
    dr_rc=$?
    rm -f -- "$dr_tmp"
    return "$dr_rc"
  }
  dr_flush "$dr_tmp" || {
    dr_rc=$?
    rm -f -- "$dr_tmp"
    return "$dr_rc"
  }
  mv -f -- "$dr_tmp" "$dr_dest" || {
    dr_rc=$?
    rm -f -- "$dr_tmp"
    return "$dr_rc"
  }
  dr_flush "$dr_dir"
}
