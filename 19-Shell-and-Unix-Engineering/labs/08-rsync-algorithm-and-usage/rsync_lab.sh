#!/bin/sh
# rsync_lab.sh WORKDIR MODE [ARG...]: drive the rsync delta model in rsync_delta.c.
# WORKDIR must already exist (the tests pass $BATS_TEST_TMPDIR); every file goes there.
#
# Probe modes (passed straight to the compiled model):
#   gen SEED LEN OUT | weak FILE | strong FILE | roll FILE BLEN | sizes LEN
# Scenario modes (build a basis and a new file, then run the three-party flow):
#   insert     one byte prepended to a 1 MiB basis
#   overwrite  one byte changed in the middle of a 1 MiB basis
#   swap       the two halves of a 1 MiB basis exchanged
#   collide    a 3-byte basis whose weak checksum collides with the new file
set -eu

work=$1
mode=$2
shift 2
here=$(cd "$(dirname "$0")" && pwd)
probe=$work/rsync_delta

build() {
  if [ ! -x "$probe" ]; then
    "${CC:-cc}" -std=c99 -O2 -Wall -Wextra -Werror -o "$probe" "$here/rsync_delta.c"
  fi
}

# head_bytes IN OFFSET OUT: copy the first OFFSET bytes of IN to OUT (POSIX dd, no head -c).
head_bytes() {
  dd if="$1" of="$3" bs="$2" count=1 2>/dev/null
}

# tail_from IN OFFSET: write IN from byte OFFSET onward to stdout.
tail_from() {
  dd if="$1" bs="$2" skip=1 2>/dev/null
}

# sync_pair BASIS NEW BLEN: signature on the receiver, delta on the sender, patch on the receiver.
sync_pair() {
  basis=$1
  new=$2
  blen=$3
  "$probe" sig "$basis" "$blen" "$work/basis.sig" >/dev/null
  "$probe" delta "$work/basis.sig" "$new" "$work/new.delta"
  "$probe" patch "$basis" "$work/new.delta" "$work/rebuilt"
  if cmp -s "$new" "$work/rebuilt"; then
    echo "rebuilt=identical"
  else
    echo "rebuilt=DIFFERENT"
  fi
  "$probe" naive "$basis" "$new" "$blen"
}

case $mode in
  gen | weak | strong | roll | sizes)
    build
    "$probe" "$mode" "$@"
    ;;
  insert)
    build
    "$probe" gen 42 1048576 "$work/basis"
    {
      printf 'Z'
      cat "$work/basis"
    } >"$work/new"
    sync_pair "$work/basis" "$work/new" 1024
    ;;
  overwrite)
    build
    "$probe" gen 42 1048576 "$work/basis"
    head_bytes "$work/basis" 500000 "$work/new"
    printf '\000' >>"$work/new"
    tail_from "$work/basis" 500001 >>"$work/new"
    sync_pair "$work/basis" "$work/new" 1024
    ;;
  swap)
    build
    "$probe" gen 42 1048576 "$work/basis"
    head_bytes "$work/basis" 524288 "$work/first"
    {
      tail_from "$work/basis" 524288
      cat "$work/first"
    } >"$work/new"
    sync_pair "$work/basis" "$work/new" 1024
    ;;
  collide)
    build
    printf 'aaa' >"$work/basis"
    printf 'b_b' >"$work/new"
    sync_pair "$work/basis" "$work/new" 3
    ;;
  *)
    echo "usage: rsync_lab.sh WORKDIR gen|weak|strong|roll|sizes|insert|overwrite|swap|collide ..." >&2
    exit 64
    ;;
esac
