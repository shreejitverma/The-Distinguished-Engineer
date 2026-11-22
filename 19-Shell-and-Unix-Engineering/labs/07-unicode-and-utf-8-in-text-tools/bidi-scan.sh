#!/bin/sh
# bidi-scan.sh: report lines that contain a Unicode bidirectional control character (UAX #9).
# These nine code points reorder how text is displayed without changing the bytes a compiler or
# interpreter reads, which is the "Trojan Source" attack (CVE-2021-42574):
#   U+202A LRE, U+202B RLE, U+202C PDF, U+202D LRO, U+202E RLO, U+2066 LRI, U+2067 RLI, U+2068 FSI, U+2069 PDI.
# The scan is byte-level (LC_ALL=C) on the UTF-8 encoding of each one, so it needs no multibyte
# support from grep and cannot be fooled by an invalid-sequence "binary file" verdict.
# It prints only line numbers: echoing the offending line would send the control character to the terminal.
# Usage: bidi-scan.sh [FILE]
# Exit status: 0 clean, 1 a control character was found, 2 usage or read error.
set -u
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != - ] && [ ! -r "$1" ]; }; then
  echo "usage: bidi-scan.sh [FILE]" >&2
  exit 2
fi
LC_ALL=C
export LC_ALL
# Octal escapes in a printf format are POSIX; each alternative is the exact 3-byte UTF-8 sequence.
pat=$(printf '\342\200\252|\342\200\253|\342\200\254|\342\200\255|\342\200\256|\342\201\246|\342\201\247|\342\201\250|\342\201\251')
if [ "$#" -eq 1 ] && [ "$1" != - ]; then
  hits=$(grep -n -E -e "$pat" <"$1")
else
  hits=$(grep -n -E -e "$pat")
fi
rc=$?
case $rc in
  0)
    printf '%s\n' "$hits" | cut -d: -f1 | while read -r n; do
      echo "bidi control character on line $n"
    done
    exit 1
    ;;
  1) exit 0 ;;
  *) exit 2 ;;
esac
