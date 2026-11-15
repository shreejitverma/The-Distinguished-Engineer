#!/bin/sh
# sweep: run a sort-like tool over every file in DIR with an unguarded glob,
# with "--", or with a "./" prefix, and report whether a file named like an
# option hijacked the tool's -o (output file) option.
# Usage: sweep.sh glob|dashdash|dotslash DIR
set -u
mode=${1:-}
dir=${2:-}

# merge [-o FILE] FILE...: concatenate FILEs to stdout, or to FILE with -o.
# A stand-in for sort -o: the option is ordinary, the operands are untrusted.
merge() {
  out=''
  OPTIND=1
  while getopts o: opt; do
    case $opt in
      o) out=$OPTARG ;;
      *) return 2 ;;
    esac
  done
  shift $((OPTIND - 1))
  [ "$#" -gt 0 ] || return 2
  if [ -n "$out" ]; then
    cat -- "$@" >"$out"
  else
    cat -- "$@"
  fi
}

case $dir in
  /*) ;;
  *)
    printf 'sweep: DIR must be absolute\n' >&2
    exit 64
    ;;
esac
cd "$dir" || exit 1
# Pathname expansion sorts in the collating order of LC_COLLATE (XCU 2.14.3);
# pin it so "-o..." sorts before "report.txt" in every locale.
LC_ALL=C
export LC_ALL

case $mode in
  glob)
    # shellcheck disable=SC2035 # the unguarded glob is the vulnerability under test
    merge *
    ;;
  dashdash) merge -- * ;;
  dotslash) merge ./* ;;
  *)
    printf 'usage: sweep.sh glob|dashdash|dotslash DIR\n' >&2
    exit 64
    ;;
esac
rc=$?
if [ -e owned ]; then
  stolen=$(cat owned) || exit 1
  printf 'clobbered: owned now holds %s\n' "$stolen"
fi
exit "$rc"
