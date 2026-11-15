#!/bin/sh
# holes.sh - shell-level sparse-file probes, run under the shell under test.
#
# Usage:
#   holes.sh compile SRC OUT          build the C probe with "${CC:-cc}"
#   holes.sh impl UTIL                print gnu, uutils, busybox, or bsd for dd or cp
#   holes.sh extend FILE BYTES        dd if=/dev/null seek=BYTES: set the size by ftruncate
#   holes.sh extend-notrunc FILE BYTES   the same with conv=notrunc
#   holes.sh zeros-sparse FILE KIB    dd conv=sparse from /dev/zero
#   holes.sh copy cp|cat SRC DST      copy a file with cp, or with cat and a redirection
#   holes.sh sizes FILE               apparent size (wc -c) and allocation (du -k)
#   holes.sh comsub-len FILE          ${#v} after v=$(cat FILE)
set -u

die() {
  printf 'holes.sh: %s\n' "$*" >&2
  exit 2
}

[ "$#" -ge 2 ] || die "usage: holes.sh MODE ARGS"
mode=$1
shift

case $mode in
  compile)
    "${CC:-cc}" -std=c99 -O2 -Wall -Wextra -Werror -o "$2" "$1"
    ;;
  impl)
    # --version is GNU and uutils; BusyBox applets name themselves in --help; BSD rejects both.
    ver=$("$1" --version 2>&1 </dev/null) || ver=""
    case $ver in
      *uutils*) echo uutils ;;
      *coreutils*) echo gnu ;; # "dd (coreutils) 9.12" and "cp (GNU coreutils) 9.12"
      *)
        help=$("$1" --help 2>&1 </dev/null) || help=""
        case $help in
          *BusyBox*) echo busybox ;;
          *) echo bsd ;;
        esac
        ;;
    esac
    ;;
  extend)
    # Empty input, so dd only opens, truncates at the seek point, and seeks: no data is written.
    dd if=/dev/null of="$1" bs=1 seek="$2" 2>/dev/null
    ;;
  extend-notrunc)
    dd if=/dev/null of="$1" bs=1 seek="$2" conv=notrunc 2>/dev/null
    ;;
  zeros-sparse)
    dd if=/dev/zero of="$1" bs=1024 count="$2" conv=sparse 2>/dev/null
    ;;
  copy)
    [ "$#" -eq 3 ] || die "usage: holes.sh copy cp|cat SRC DST"
    case $1 in
      cp) cp "$2" "$3" ;;
      cat) cat "$2" >"$3" ;;
      *) die "unknown copy method: $1" ;;
    esac
    ;;
  sizes)
    # BSD wc pads the count with blanks; du prints "KIB<tab>NAME".
    apparent=$(wc -c <"$1" | tr -d ' ') || exit 1
    du_line=$(du -k "$1") || exit 1
    printf 'apparent=%s du_k=%s\n' "$apparent" "${du_line%%[!0-9]*}"
    ;;
  comsub-len)
    v=$(cat "$1")
    printf 'len=%s\n' "${#v}"
    ;;
  *)
    die "unknown mode: $mode"
    ;;
esac
