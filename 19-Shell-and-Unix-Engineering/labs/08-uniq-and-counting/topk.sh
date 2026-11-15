#!/bin/sh
# topk.sh [N]: print the N (default 10) most frequent input lines as COUNT<TAB>LINE.
# Ties are broken by the line itself in ascending byte order, so the output is deterministic.
# The line is kept byte for byte, including leading blanks, which a `read count line` parse would strip.
# Usage: topk.sh [N] <input
# Example: printf 'b\na\nb\n' | topk.sh 2  ->  2<TAB>b, then 1<TAB>a
set -eu

n=${1:-10}
case $n in
  '' | *[!0-9]*)
    printf 'topk.sh: N must be a decimal integer, got "%s"\n' "$n" >&2
    exit 2
    ;;
  *) ;;
esac

# Byte order for both sorts and byte identity for uniq: no locale can make two different lines collate equal.
LC_ALL=C
export LC_ALL
tab=$(printf '\t')

# 1. sort makes equal lines adjacent; uniq only ever compares a line with the one before it.
# 2. uniq -c right-aligns the count (width 7 in GNU, BusyBox, and uutils; 4 in BSD and macOS), then one space.
# 3. sed turns "<blanks>COUNT<space>" into "COUNT<tab>", leaving the line's own leading blanks alone.
# 4. sort by count descending, then by the rest of the line ascending; plain `sort -rn` would reverse ties too.
sort \
  | uniq -c \
  | sed "s/^ *\\([0-9][0-9]*\\) /\\1$tab/" \
  | sort -t "$tab" -k1,1nr -k2 \
  | head -n "$n"
