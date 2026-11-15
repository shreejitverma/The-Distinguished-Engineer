#!/bin/sh
# converge.sh ROOT: bring ROOT to one desired state from any starting state, any number of times.
# Each ensure_* step reads the current state, acts only when it differs from the desired state,
# and prints "ok WHAT" or "changed WHAT"; the last line is "changed=N".
# A rerun after a crash at any point converges to the same final state.
# CONVERGE_CRASH=NAME kills this shell with SIGKILL at crash point NAME, so no trap can run.
# Exit status: 0 when ROOT ends in the desired state (changed or not), 1 on any failure.
# Usage: converge.sh ROOT
set -u

die() {
  printf 'converge: %s\n' "$*" >&2
  exit 1
}

changed=0

# report STATE WHAT: print one result line and count the changes.
report() {
  printf '%s %s\n' "$1" "$2"
  if [ "$1" = changed ]; then
    changed=$((changed + 1))
  fi
}

# crashpoint NAME: die by SIGKILL here when CONVERGE_CRASH names this point.
crashpoint() {
  if [ "${CONVERGE_CRASH-}" = "$1" ]; then
    kill -s KILL "$$"
  fi
}

# ensure_dir DIR: DIR exists as a directory.
ensure_dir() {
  if [ -d "$1" ]; then
    report ok "dir $1"
    return 0
  fi
  mkdir -p "$1" || die "cannot create directory $1"
  report changed "dir $1"
}

# ensure_line FILE LINE: FILE contains LINE as a whole line, exactly once if we added it.
ensure_line() {
  if [ -f "$1" ]; then
    grep -qxF -e "$2" "$1"
    st=$?
    case $st in
      0)
        report ok "line $1"
        return 0
        ;;
      1) ;; # absent: append below
      *) die "cannot read $1 (grep status $st)" ;;
    esac
    # A last byte that is not a newline would glue LINE onto the last line.
    last=$(tail -c 1 "$1") || die "cannot read $1"
    if [ -n "$last" ]; then
      printf '\n' >>"$1" || die "cannot append to $1"
      crashpoint line-newline
    fi
  fi
  printf '%s\n' "$2" >>"$1" || die "cannot append to $1"
  report changed "line $1"
}

# ensure_file DEST CMD [ARG...]: DEST holds exactly the bytes CMD prints.
# The output is staged next to DEST and renamed over it only when the bytes differ,
# so an unchanged file keeps its inode and mtime, and a failing CMD never truncates DEST.
ensure_file() {
  dest=$1
  shift
  if [ -d "$dest" ]; then
    die "$dest is a directory"
  fi
  case $dest in
    */*) stage="${dest%/*}/.${dest##*/}.new" ;;
    *) stage=".$dest.new" ;;
  esac
  "$@" >"$stage" || die "cannot render $dest"
  crashpoint file-staged
  if [ -f "$dest" ] && cmp -s "$stage" "$dest"; then
    rm -f "$stage"
    report ok "file $dest"
    return 0
  fi
  mv -f "$stage" "$dest" || die "cannot install $dest"
  report changed "file $dest"
}

# ensure_symlink TARGET LINK: LINK is a symbolic link whose contents are TARGET.
# Never "ln -s" over an existing link: if LINK resolves to a directory, ln creates the new
# link inside that directory and exits 0.
ensure_symlink() {
  if [ -L "$2" ]; then
    cur=$(readlink "$2") || die "cannot read link $2"
    if [ "$cur" = "$1" ]; then
      report ok "link $2"
      return 0
    fi
    rm -f "$2" || die "cannot remove link $2"
  elif [ -e "$2" ]; then
    die "$2 exists and is not a symbolic link"
  fi
  crashpoint link-removed
  ln -s "$1" "$2" || die "cannot create link $2"
  report changed "link $2"
}

# ensure_absent PATH: nothing exists at PATH (a directory is refused, not removed).
ensure_absent() {
  if [ ! -e "$1" ] && [ ! -L "$1" ]; then
    report ok "absent $1"
    return 0
  fi
  rm -f "$1" || die "cannot remove $1"
  report changed "absent $1"
}

[ "$#" -eq 1 ] || die "usage: converge.sh ROOT"
cd "$1" || die "cannot enter $1"

ensure_dir conf
crashpoint after-dir
ensure_line hosts '10.0.0.1 db'
crashpoint after-line
ensure_file conf/app.conf sed 's/^port=80$/port=8080/' skel/app.conf
crashpoint after-file
ensure_symlink releases/v1 current
crashpoint after-link
ensure_absent run/stale.pid
printf 'changed=%d\n' "$changed"
