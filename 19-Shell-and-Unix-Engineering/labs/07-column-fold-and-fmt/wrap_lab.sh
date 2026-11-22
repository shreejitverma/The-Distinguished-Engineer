#!/bin/sh
# wrap_lab.sh - probes for fold, fmt, and column.
#
# Usage: wrap_lab.sh PROBE CMD [ARG...]
#   PROBE names one experiment below; CMD [ARG...] is the utility under test,
#   for example "fold", "gfold", "busybox fold", "fmt", "gfmt", or "column".
#
# Each probe writes a fixed input to a file, runs the utility on it with a chosen
# locale, and prints one line: the output rendered by vis (or a list of line
# lengths), then " status=N" with the utility's exit status.
#
# vis maps bytes so that every output is one line of ASCII:
#   newline -> |   tab -> >   carriage return -> ^   0xff -> #
#   U+00E9 (bytes 0xc3 0xa9) -> []      U+4E2D (bytes 0xe4 0xb8 0xad) -> {~}
# so a character split across two lines shows up as a broken bracket pair.
# Scratch files live in a mktemp -d directory under TMPDIR, removed on exit.

if [ "$#" -lt 2 ]; then
  echo "usage: wrap_lab.sh PROBE CMD [ARG...]" >&2
  exit 2
fi
probe=$1
shift

: "${TMPDIR:?wrap_lab.sh needs TMPDIR}"
work=$(mktemp -d "$TMPDIR/wrap_lab.XXXXXX") || exit 1
trap 'rm -rf "$work"' EXIT

in=$work/in
out=$work/out
status=0

# run_tool LOCALE CMD [ARG...]: run CMD on $in with LC_ALL=LOCALE in its
# environment only, capture stdout in $out, and keep its exit status.
run_tool() {
  loc=$1
  shift
  LC_ALL=$loc "$@" <"$in" >"$out" 2>"$work/err"
  status=$?
}

vis() {
  LC_ALL=C tr '\303\251\344\270\255\377\n\t\r' '[]{~}#|>^' <"$out"
}

# lengths: byte length of each output line, space separated (inputs are ASCII).
lengths() {
  LC_ALL=C awk '{ n = n (NR > 1 ? " " : "") length($0) } END { print n }' "$out"
}

# report RENDER: print the output as RENDER (vis or lengths) shows it, then the status.
report() {
  shown=$("$1")
  printf '%s status=%s\n' "$shown" "$status"
}

# A paragraph of 20 ASCII words, 131 bytes plus a newline.
para='one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'

case $probe in
  fold-ascii)
    printf 'abcdefghij\n' >"$in"
    run_tool C "$@" -w 4
    report vis
    ;;
  fold-spaces)
    printf 'the quick brown fox\n' >"$in"
    run_tool C "$@" -s -w 10
    report vis
    ;;
  fold-tab)
    printf 'a\tbcdefghijk\n' >"$in"
    run_tool C "$@" -w 10
    report vis
    ;;
  fold-cr)
    printf 'abcdef\rghijkl\n' >"$in"
    run_tool C "$@" -w 8
    report vis
    ;;
  fold-noeol)
    printf 'abcdefg' >"$in"
    run_tool C "$@" -w 3
    report vis
    ;;
  fold-utf8)
    printf '\303\251\303\251\303\251\303\251\303\251\303\251\n' >"$in"
    run_tool C.UTF-8 "$@" -w 3
    report vis
    ;;
  fold-c-locale)
    printf '\303\251\303\251\303\251\303\251\303\251\303\251\n' >"$in"
    run_tool C "$@" -w 3
    report vis
    ;;
  fold-wide)
    printf '\344\270\255\344\270\255\344\270\255\n' >"$in"
    run_tool C.UTF-8 "$@" -w 4
    report vis
    ;;
  fold-bytes)
    printf '\303\251x\n' >"$in"
    run_tool C.UTF-8 "$@" -b -w 1
    report vis
    ;;
  fold-invalid)
    printf 'ab\377cd\nef\n' >"$in"
    run_tool C.UTF-8 "$@" -w 80
    report vis
    ;;
  fmt-default)
    printf '%s\n' "$para" >"$in"
    run_tool C "$@"
    report lengths
    ;;
  fmt-exact)
    printf 'aaaa bbbb\n' >"$in"
    run_tool C "$@" -w 9
    report vis
    ;;
  fmt-crlf)
    printf 'aa bb\r\ncc dd\r\n' >"$in"
    run_tool C "$@"
    report vis
    ;;
  fmt-utf8)
    printf '\303\251\303\251\303\251 \303\251\303\251\303\251 \303\251\303\251\303\251 \303\251\303\251\303\251\n' >"$in"
    run_tool C.UTF-8 "$@" -w 12
    report vis
    ;;
  fmt-c-locale)
    printf 'caf\303\251 ok\n' >"$in"
    run_tool C "$@"
    report vis
    ;;
  fmt-indent)
    printf '        aa bb\n        cc dd\n' >"$in"
    run_tool C "$@"
    report vis
    ;;
  fmt-dot)
    printf 'aa bb\n.TH x\ncc dd\n' >"$in"
    run_tool C "$@"
    report vis
    ;;
  fmt-sentence)
    printf 'One.\nTwo. Three.  Four\n' >"$in"
    run_tool C "$@"
    report vis
    ;;
  column-fill | column-fillrows)
    printf '%s\n' a b c d e f g h >"$in"
    if [ "$probe" = column-fill ]; then
      run_tool C "$@" -c 30
    else
      run_tool C "$@" -x -c 30
    fi
    report vis
    ;;
  column-env)
    printf '%s\n' a b c d e f g h >"$in"
    COLUMNS=20
    export COLUMNS
    run_tool C "$@"
    report vis
    ;;
  column-table)
    printf 'name size\nalpha 1\n\nb 22222\n' >"$in"
    run_tool C "$@" -t
    sed 's/ *$//' "$out" >"$work/trim" && mv "$work/trim" "$out"
    report vis
    ;;
  column-empty-field)
    printf 'a,b,c\n1,,3\n' >"$in"
    run_tool C "$@" -t -s ,
    sed 's/ *$//' "$out" >"$work/trim" && mv "$work/trim" "$out"
    report vis
    ;;
  column-noeol)
    printf 'a b\ncc d' >"$in"
    run_tool C "$@" -t
    sed 's/ *$//' "$out" >"$work/trim" && mv "$work/trim" "$out"
    report vis
    ;;
  column-invalid)
    printf 'a\377b x\nc y\n' >"$in"
    run_tool C.UTF-8 "$@" -t
    sed 's/ *$//' "$out" >"$work/trim" && mv "$work/trim" "$out"
    report vis
    ;;
  column-long-line)
    # One 3000-byte line (above BSD column's LINE_MAX of 2048), then "short".
    awk 'BEGIN { while (n++ < 3000) printf "x"; printf "\nshort\n" }' >"$in"
    run_tool C "$@" -c 80
    report lengths
    ;;
  *)
    echo "wrap_lab.sh: unknown probe: $probe" >&2
    exit 2
    ;;
esac
