#!/bin/sh
# probe.sh: observe how the sort(1) on PATH behaves when it sorts externally.
#
# Usage: probe.sh MODE [ARG]
#   spill SIZE      sort 200 lines with -S SIZE and a TMPDIR that does not exist
#   temps BATCH     count temporary files written and read with -S 1b --batch-size=BATCH
#   longprog        spill through a --compress-program whose path is 200 bytes long
#   merge-unsorted  merge an unsorted file with -m, then check it with -c
#   inplace         sort -o f f where f has a second hard link
#   merge-alias     sort -m -o ./x x y, naming the output differently from the input
#   predict         plant a symlink at the predictable name .bsdsort.PID.0
#   batch-limit     sort --batch-size=100 under ulimit -n 20
set -u

here=$(cd "$(dirname "$0")" && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/probe.XXXXXX")
trap 'rm -rf "$scratch"' EXIT

# 200 distinct three-digit lines in a scrambled order (37 is coprime to 200).
awk 'BEGIN { for (i = 0; i < 200; i++) printf "%03d\n", (i * 37) % 200 }' >"$scratch/in"

# lines FILE: print the lines of FILE joined by single spaces.
lines() {
  tr '\n' ' ' <"$1" | sed 's/ $//'
}

case ${1-} in
  spill)
    TMPDIR=$scratch/missing sort -S "$2" "$scratch/in" >"$scratch/out" 2>/dev/null
    printf 'status=%s\n' "$?"
    ;;
  temps)
    # A short name found through PATH keeps the compressor command line short.
    mkdir "$scratch/bin"
    cp "$here/count-compress.sh" "$scratch/bin/cz"
    PATH=$scratch/bin:$PATH
    COUNT_LOG=$scratch/log
    export COUNT_LOG
    : >"$COUNT_LOG"
    TMPDIR=$scratch sort -S 1b --batch-size="$2" --compress-program=cz \
      "$scratch/in" >"$scratch/out" 2>/dev/null
    st=$?
    w=$(grep -c write "$COUNT_LOG")
    r=$(grep -c read "$COUNT_LOG")
    if LC_ALL=C sort -c "$scratch/out" 2>/dev/null; then sorted=yes; else sorted=no; fi
    printf 'status=%s written=%s read=%s sorted=%s\n' "$st" "$w" "$r" "$sorted"
    ;;
  longprog)
    # One directory name of 186 bytes makes the program path at least 200 bytes.
    d=$scratch/$(awk 'BEGIN { while (n++ < 186) printf "d" }')
    mkdir "$d"
    cp "$here/count-compress.sh" "$d/cz"
    COUNT_LOG=$scratch/log
    export COUNT_LOG
    TMPDIR=$scratch sort -S 1b --compress-program="$d/cz" \
      "$scratch/in" >"$scratch/out" 2>/dev/null
    st=$?
    n=$(wc -l <"$scratch/out" | tr -d ' ')
    printf 'status=%s lines=%s\n' "$st" "$n"
    ;;
  merge-unsorted)
    printf 'b\na\n' >"$scratch/x"
    printf 'c\n' >"$scratch/y"
    LC_ALL=C sort -m "$scratch/x" "$scratch/y" >"$scratch/out"
    LC_ALL=C sort -c "$scratch/x" 2>/dev/null
    check=$?
    merged=$(lines "$scratch/out")
    printf 'merged=%s check=%s\n' "$merged" "$check"
    ;;
  inplace)
    umask 022
    printf 'b\na\n' >"$scratch/f"
    chmod 644 "$scratch/f"
    ln "$scratch/f" "$scratch/g"
    LC_ALL=C sort -o "$scratch/f" "$scratch/f"
    if cmp -s "$scratch/f" "$scratch/g"; then link='shared'; else link='split'; fi
    # shellcheck disable=SC2012 # the name is fixed; only the mode string is read
    mode=$(ls -l "$scratch/f" | cut -c1-10)
    f=$(lines "$scratch/f")
    printf 'f=%s link=%s mode=%s\n' "$f" "$link" "$mode"
    ;;
  merge-alias)
    printf 'a\nc\n' >"$scratch/x"
    printf 'b\n' >"$scratch/y"
    (cd "$scratch" && LC_ALL=C sort -m -o ./x x y)
    x=$(lines "$scratch/x")
    printf 'x=%s\n' "$x"
    ;;
  predict)
    mkdir "$scratch/t"
    printf 'precious\n' >"$scratch/victim"
    # $$ in sh -c is the pid that exec hands to sort, so the name is predictable.
    # shellcheck disable=SC2016 # $$ and $1 belong to the inner sh
    TMPDIR=$scratch/t sh -c 'ln -s "$1/victim" "$TMPDIR/.bsdsort.$$.0"; exec sort -S 1b "$1/in"' \
      sh "$scratch" >"$scratch/out" 2>/dev/null
    victim=$(cat "$scratch/victim")
    if [ "$victim" = precious ]; then v=intact; else v=overwritten; fi
    printf 'victim=%s\n' "$v"
    ;;
  batch-limit)
    # shellcheck disable=SC3045 # POSIX.1-2024 XCU ulimit specifies -n
    (ulimit -n 20 && exec sort --batch-size=100 "$scratch/in") >"$scratch/out" 2>"$scratch/err"
    st=$?
    printf 'status=%s\n' "$st"
    sed -n 's/.*\(with current rlimit is [0-9]*\).*/\1/p' "$scratch/err"
    ;;
  *)
    printf 'usage: probe.sh MODE [ARG]\n' >&2
    exit 2
    ;;
esac
