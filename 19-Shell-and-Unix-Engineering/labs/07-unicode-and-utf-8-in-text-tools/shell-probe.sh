#!/bin/sh
# shell-probe.sh: how the shell that runs this script measures a UTF-8 string.
# Usage: shell-probe.sh      (the caller exports LC_ALL before starting the shell, see utf8.bats)
# It prints key=value lines, each computed here:
#   locale  the LC_ALL the shell was started with
#   len     ${#x} for x = "caf" followed by U+00E9 (4 characters, 5 bytes)
#   dq      yes if this shell expands the dollar-single-quote form, no if it leaves the dollar literal
#   pad     bytes or chars: which unit printf '%-6s' pads in, from the byte length of its output
#   prec    bytes or chars: which unit printf '%.1s' cuts in, from the byte length of its output
set -u
e=$(printf '\303\251')
x=$(printf 'caf\303\251')
echo "locale=${LC_ALL-unset}"
echo "len=${#x}"
# The dollar-single-quote form is parsed by the shell, so build the probe inside eval to keep
# the script itself parseable by shells that lack it.
dq=$(eval 'printf %s $'\''a'\''' 2>/dev/null)
if [ "$dq" = a ]; then echo dq=yes; else echo dq=no; fi
# U+00E9 is 2 bytes: padded to width 6 plus "|" the output is 7 bytes if the width counts bytes
# (4 spaces) and 8 bytes if it counts characters (5 spaces).
n=$(printf '%-6s|' "$e" | wc -c | tr -d ' ')
case $n in
  7) echo pad=bytes ;;
  8) echo pad=chars ;;
  *) echo "pad=other($n)" ;;
esac
# '%.1s' of U+00E9 followed by "x" is 1 byte if the precision counts bytes, 2 if characters.
n=$(printf '%.1s' "${e}x" | wc -c | tr -d ' ')
case $n in
  1) echo prec=bytes ;;
  2) echo prec=chars ;;
  *) echo "prec=other($n)" ;;
esac
