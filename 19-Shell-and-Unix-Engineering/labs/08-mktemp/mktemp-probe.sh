#!/bin/sh
# mktemp-probe: measure how a mktemp implementation and the shell under test
# create, protect, and clean up temporary files and directories.
# Every path it creates is under DIR; CMD is the mktemp command to test,
# for example "mktemp", "gmktemp", or "busybox mktemp" passed as two words.
#
# Usage: mktemp-probe.sh MODE DIR [ARG...]
#   modes DIR CMD...          mode bits of a new file and directory under umask 022 and 0277
#   try DIR TEMPLATE CMD...   run CMD DIR/TEMPLATE; print "ok NAME" or "fail"
#   default DIR CMD...        run CMD -p DIR with no template; print the name it chose
#   emptyp DIR CMD...         TMPDIR=DIR CMD -u -p '' x.XXXXXX; where does the name land?
#   scratch DIR CMD...        the portable idiom: an absolute template under TMPDIR, -d, 6 Xs
#   noclobber DIR             atomic exclusive create with set -C, no mktemp at all
#   cleanup DIR CMD...        an EXIT trap removes the scratch directory; exit 3 survives
#   leak DIR CMD...           only an EXIT trap, then SIGTERM: does the directory survive?
#   forward DIR CMD...        an EXIT trap plus a TERM trap that calls exit 143
set -u

mode=${1:?mode}
dir=${2:?dir}
shift 2

# perm_of PATH: print the ls -l mode string, without a trailing ACL or xattr marker.
perm_of() {
  p=$(ls -ld "$1") || return 1
  p=${p%% *}
  p=${p%[@+.]}
  printf '%s\n' "$p"
}

case $mode in
  modes)
    for mask in 022 0277; do
      f=$(
        umask "$mask"
        "$@" "$dir/f$mask.XXXXXX"
      ) || exit 1
      d=$(
        umask "$mask"
        "$@" -d "$dir/d$mask.XXXXXX"
      ) || exit 1
      fp=$(perm_of "$f") || exit 1
      dp=$(perm_of "$d") || exit 1
      printf '%s %s %s\n' "$mask" "$fp" "$dp"
    done
    ;;

  try)
    tmpl=${1:?template}
    shift
    if out=$("$@" "$dir/$tmpl" 2>/dev/null); then
      printf 'ok %s\n' "${out##*/}"
    else
      printf 'fail\n'
    fi
    ;;

  default)
    out=$("$@" -p "$dir") || exit 1
    case $out in
      "$dir"/*) printf '%s\n' "${out##*/}" ;;
      *) printf 'elsewhere %s\n' "$out" ;;
    esac
    ;;

  emptyp)
    out=$(TMPDIR=$dir "$@" -u -p '' x.XXXXXX) || exit 1
    case $out in
      "$dir"/x.*) printf 'in-TMPDIR\n' ;;
      /x.*) printf 'root\n' ;;
      *) printf 'other %s\n' "$out" ;;
    esac
    ;;

  scratch)
    TMPDIR=$dir
    export TMPDIR
    tmp=$("$@" -d "${TMPDIR:-/tmp}/probe.XXXXXX") || exit 1
    p=$(perm_of "$tmp") || exit 1
    case $tmp in
      "$dir"/probe.??????) where=under-TMPDIR ;;
      *) where=elsewhere ;;
    esac
    printf '%s %s\n' "$where" "$p"
    ;;

  noclobber)
    f=$dir/lock
    set -C
    if printf 'first\n' >"$f"; then r1=created; else r1=refused; fi
    if { printf 'second\n' >"$f"; } 2>/dev/null; then r2=overwrote; else r2=refused; fi
    c1=$(cat "$f")
    printf 'third\n' >|"$f"
    c2=$(cat "$f")
    set +C
    printf '%s %s %s %s\n' "$r1" "$r2" "$c1" "$c2"
    ;;

  cleanup)
    tmp=$("$@" -d "$dir/probe.XXXXXX") || exit 1
    trap 'rm -rf -- "$tmp"' EXIT
    printf 'data\n' >"$tmp/payload"
    printf '%s\n' "$tmp"
    exit 3
    ;;

  leak | forward)
    tmp=$("$@" -d "$dir/probe.XXXXXX") || exit 1
    printf '%s\n' "$tmp"
    trap 'rm -rf -- "$tmp"' EXIT
    if [ "$mode" = forward ]; then
      trap 'exit 143' HUP INT TERM
    fi
    (
      sleep 1
      kill -TERM "$$"
    ) &
    sleep 2
    ;;

  *)
    printf 'unknown mode: %s\n' "$mode" >&2
    exit 2
    ;;
esac
