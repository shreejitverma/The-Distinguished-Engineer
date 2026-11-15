#!/bin/sh
# ddprobe: measure what a dd implementation does with short reads, block
# accounting, skip and seek, number syntax, conversions, and status signals.
# Usage: ddprobe.sh MODE
#   MODE is one of: flavor shortread records skip skipeof seek numbers conv
#   spelling signal
# DD names the dd to test (default "dd"); it may be two words, such as
# "busybox dd". Scratch files live in a fresh mktemp -d directory under
# TMPDIR and are removed on exit.
set -u
DD=${DD:-dd}

# dd_ ARG...: run the dd under test with ARGs.
dd_() {
  eval "$DD"' "$@"'
}

# count_bytes: print the number of bytes on stdin with no padding.
count_bytes() {
  wc -c | tr -d ' \t'
}

# dd_flavor: print gnu, uutils, busybox, bsd, or unknown, from how dd
# answers --version (BSD dd has no options and calls it an unknown operand).
dd_flavor() {
  v=$(dd_ --version 2>&1 </dev/null) || :
  case $v in
    *uutils*) echo uutils ;;
    *coreutils*) echo gnu ;;
    *BusyBox*) echo busybox ;;
    *"unknown operand"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# slow_writer FIRST SECOND: write FIRST, pause one second, write SECOND, so a
# reader that is already waiting gets FIRST as one short read.
slow_writer() {
  {
    printf '%s' "$1"
    /bin/sleep 1
    printf '%s' "$2"
  } 2>/dev/null
}

# shortread: bytes copied by bs=8 count=1 from a pipe, without and with
# iflag=fullblock.
mode_shortread() {
  plain=$(slow_writer aaaa bbbb | dd_ bs=8 count=1 2>/dev/null | count_bytes)
  full=$(slow_writer aaaa bbbb | dd_ bs=8 count=1 iflag=fullblock 2>/dev/null | count_bytes)
  printf 'plain=%s fullblock=%s\n' "$plain" "$full"
}

# recs LABEL ARG...: copy two short reads and print the records lines.
recs() {
  label=$1
  shift
  slow_writer aaaa bbbb | dd_ "$@" 2>"$tmp/err" >/dev/null
  rin=$(sed -n 's/ records in$//p' "$tmp/err")
  rout=$(sed -n 's/ records out$//p' "$tmp/err")
  printf '%s: in=%s out=%s\n' "$label" "$rin" "$rout"
}

# records: whole+partial counts for ibs=8 obs=8, bs=8, and the defaults.
mode_records() {
  recs ibs-obs ibs=8 obs=8
  recs bs bs=8
  recs default
}

# skip: skip=1 with bs=4 on a pipe whose first read is 2 bytes short.
mode_skip() {
  plain=$(slow_writer ab cdefgh | dd_ bs=4 skip=1 2>/dev/null)
  full=$(slow_writer ab cdefgh | dd_ bs=4 skip=1 iflag=fullblock 2>/dev/null)
  printf 'plain=%s fullblock=%s\n' "$plain" "$full"
}

# skipeof: exit status when skip= runs past the end of a pipe.
mode_skipeof() {
  printf ab | dd_ bs=4 skip=1 >/dev/null 2>&1
  echo "status=$?"
}

# seek: what seek= keeps and truncates in an existing file, and how an empty
# copy with seek= grows a short file.
mode_seek() {
  f=$tmp/f
  printf 0123456789 >"$f"
  printf AB | dd_ of="$f" bs=1 seek=2 2>/dev/null
  trunc=$(cat "$f")
  printf 0123456789 >"$f"
  printf AB | dd_ of="$f" bs=1 seek=2 conv=notrunc 2>/dev/null
  notrunc=$(cat "$f")
  printf 0123 >"$f"
  dd_ of="$f" bs=1 seek=8 </dev/null 2>/dev/null
  grow=$(count_bytes <"$f")
  nuls=$(tr -cd '\000' <"$f" | count_bytes)
  printf 'trunc=%s notrunc=%s grow=%s nuls=%s\n' "$trunc" "$notrunc" "$grow" "$nuls"
}

# numbers: bytes copied for operands whose syntax the implementations read
# differently; a rejected operand copies 0 bytes.
mode_numbers() {
  octal=$(dd_ if=/dev/zero bs=1 count=010 2>/dev/null | count_bytes)
  hex=$(dd_ if=/dev/zero bs=0x10 count=1 2>/dev/null | count_bytes)
  product=$(dd_ if=/dev/zero bs=2x4 count=1 2>/dev/null | count_bytes)
  big_b=$(dd_ if=/dev/zero bs=1 count=2B 2>/dev/null | count_bytes)
  word=$(dd_ if=/dev/zero bs=1w count=1 2>/dev/null | count_bytes)
  zero=$(printf abc | dd_ count=0 2>/dev/null | count_bytes)
  printf 'octal=%s hex=%s product=%s B=%s w=%s zero=%s\n' \
    "$octal" "$hex" "$product" "$big_b" "$word" "$zero"
}

# conv: swab on an odd length, ucase, and sync padding of a 3-byte read.
mode_conv() {
  swab=$(printf abc | dd_ conv=swab 2>/dev/null)
  ucase=$(printf abc | dd_ conv=ucase 2>/dev/null)
  padded=$(printf abc | dd_ bs=8 conv=sync 2>/dev/null | count_bytes)
  nuls=$(printf abc | dd_ bs=8 conv=sync 2>/dev/null | tr -cd '\000' | count_bytes)
  printf 'swab=%s ucase=%s sync=%s/%s\n' "$swab" "$ucase" "$padded" "$nuls"
}

# spelling: whether dd accepts the POSIX.1-2024 text's iflags= spelling and
# the iflag= spelling every implementation uses.
mode_spelling() {
  if printf x | dd_ iflags=fullblock >/dev/null 2>&1; then s=accepted; else s=rejected; fi
  if printf x | dd_ iflag=fullblock >/dev/null 2>&1; then t=accepted; else t=rejected; fi
  printf 'iflags=%s iflag=%s\n' "$s" "$t"
}

# signal SIG: send signal SIG (USR1 or INFO) to a dd blocked reading a FIFO,
# then let it reach EOF. Prints "survived" or "killed" and how many statistics
# reports dd wrote.
mode_signal() {
  sig=$1
  mkfifo "$tmp/fifo" || exit 1
  # Start dd itself as the background job (not a function, which would be a
  # subshell), so $! is the dd process that the signal must reach.
  eval "$DD"' if="$tmp/fifo" of="$tmp/out" bs=1 2>"$tmp/err" &'
  pid=$!
  # O_RDWR does not block on open, so a dd that fails to start cannot hang us.
  exec 3<>"$tmp/fifo"
  printf x >&3
  # A byte in the output proves dd is in its copy loop, past signal setup.
  tries=100
  while [ ! -s "$tmp/out" ]; do
    tries=$((tries - 1))
    [ "$tries" -gt 0 ] || break
    /bin/sleep 0.1
  done
  kill -s "$sig" "$pid" 2>/dev/null
  /bin/sleep 1
  exec 3>&-
  {
    wait "$pid"
    st=$?
  } 2>/dev/null
  if [ "$st" -eq 0 ]; then state=survived; else state=killed; fi
  reports=$(grep -c 'records in' "$tmp/err") || :
  printf '%s reports=%s\n' "$state" "$reports"
}

tmp=$(mktemp -d "${TMPDIR:-/tmp}/ddprobe.XXXXXX") || exit 1
trap 'rm -rf "$tmp"' EXIT

case ${1-} in
  flavor) dd_flavor ;;
  shortread) mode_shortread ;;
  records) mode_records ;;
  skip) mode_skip ;;
  skipeof) mode_skipeof ;;
  seek) mode_seek ;;
  numbers) mode_numbers ;;
  conv) mode_conv ;;
  spelling) mode_spelling ;;
  signal) mode_signal "${2:-USR1}" ;;
  *)
    echo "usage: ddprobe.sh flavor|shortread|records|skip|skipeof|seek|numbers|conv|spelling|signal SIG" >&2
    exit 2
    ;;
esac
