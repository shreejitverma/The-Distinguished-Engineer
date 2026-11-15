#!/bin/sh
# hold-space.sh MODE [DIR]: probes of the sed pattern space and hold space.
# Every mode runs the sed found on PATH; the shell under test never changes which sed runs.
# Line-oriented results are joined with "|" so empty lines stay visible;
# byte-exact results are printed as hex so a missing final newline or a NUL byte stays visible.
#
# Modes:
#   idioms   classic two-buffer programs (tac, join, tail, uniq, line-before-match)
#   firstx   x on line 1 swaps in the empty hold space
#   restart  D restarts the cycle without reading input
#   nlast    N on the last line, with and without POSIXLY_CORRECT, and the $!N guard
#   noeol    which newline x and G write after an unterminated last line
#   nul      a NUL byte inside a line
#   tflag    whether N resets the flag that t tests
#   files    hold space across file operands and across -i files (needs DIR)
#   big      a 1001999-byte hold space (needs DIR)

set -u

# join: print stdin lines joined with "|" on one line.
join() {
  awk '{ printf "%s%s", (NR > 1 ? "|" : ""), $0 } END { print "" }'
}

# hex: print stdin as space-separated hex bytes on one line.
hex() {
  od -An -tx1 | awk '{ for (i = 1; i <= NF; i++) printf "%s%s", (n++ ? " " : ""), $i } END { print "" }'
}

mode=${1-}

case $mode in
  idioms)
    printf 'tac: '
    printf '1\n2\n3\n' | sed '1!G;h;$!d' | join
    printf 'double: '
    printf 'a\nb\n' | sed G | join
    printf 'commas: '
    printf 'a\nb\nc\n' | sed '1h;1!H;$!d;x;s/\n/,/g' | join
    printf 'pairs: '
    printf '1\n2\n3\n4\n5\n' | sed '$!N;s/\n/ /' | join
    printf 'tail2: '
    printf '1\n2\n3\n4\n5\n' | sed '$!N;$!D' | join
    printf 'uniq: '
    printf 'a\na\nb\nb\nb\nc\na\n' | sed '$!N;/^\(.*\)\n\1$/!P;D' | join
    printf 'before: '
    printf 'a\nb\nc\nd\nc\n' | sed -n '/^c/{x;p;x;};h' | join
    printf 'odd: '
    printf '1\n2\n3\n4\n5\n' | sed 'n;d' | join
    ;;
  firstx)
    printf 'a\nb\n' | sed x | hex
    ;;
  restart)
    printf '1\n2\n3\n' | sed -n '$!N;=;P;D' | join
    ;;
  nlast)
    printf 'N: '
    printf 'a\nb\nc\n' | sed N | join
    printf 'N posixly: '
    printf 'a\nb\nc\n' | POSIXLY_CORRECT=1 sed N | join
    printf 'guarded: '
    printf 'a\nb\nc\n' | sed '$!N' | join
    ;;
  noeol)
    printf 'x: '
    printf 'a\nb' | sed x | hex
    printf 'G: '
    printf 'a' | sed G | hex
    ;;
  nul)
    printf 'lines: '
    printf 'a\000b\n' | sed -n '$='
    printf 'G: '
    printf 'a\000b\n' | sed G | hex
    ;;
  tflag)
    printf 'a\nb\n' | sed -e 's/a/A/;N;t yes' -e 's/$/ no/;b' -e ':yes' -e 's/$/ yes/' | join
    ;;
  files)
    work=$(mktemp -d "${2:?files needs DIR}/hs.XXXXXX") || exit 1
    printf '1\n' >"$work/f1"
    printf '2\n' >"$work/f2"
    printf 'stream: '
    sed x "$work/f1" "$work/f2" | hex
    # -i.bak (suffix attached) is the one spelling GNU, BSD, and BusyBox sed all accept.
    sed -i.bak x "$work/f1" "$work/f2" || exit 1
    printf 'in-place f1: '
    hex <"$work/f1"
    printf 'in-place f2: '
    hex <"$work/f2"
    ;;
  big)
    work=$(mktemp -d "${2:?big needs DIR}/hs.XXXXXX") || exit 1
    # 2000 lines of 500 bytes: the hold space ends at 1001999 bytes (1002000 with the output newline), 122x the POSIX minimum of 8192.
    awk 'BEGIN { s = sprintf("%500s", ""); gsub(/ /, "x", s); for (i = 0; i < 2000; i++) print s }' >"$work/in"
    # shellcheck disable=SC2016 # ${ is the sed last-line address followed by a block, not a parameter
    sed -n '1h;1!H;${x;s/\n/,/g;p;}' "$work/in" >"$work/out" || exit 1
    printf 'bytes: '
    wc -c <"$work/out" | tr -d ' '
    printf 'lines: '
    wc -l <"$work/out" | tr -d ' '
    ;;
  *)
    printf 'usage: %s idioms|firstx|restart|nlast|noeol|nul|tflag|files DIR|big DIR\n' "$0" >&2
    exit 2
    ;;
esac
