#!/bin/sh
# utf8-validate.sh: strict UTF-8 validator for a file or standard input, independent of the C library.
# It implements the byte-range table of RFC 3629 section 4 (Unicode Table 3-7) in awk, so it
# rejects overlong forms, UTF-16 surrogates, code points above U+10FFFF and truncated sequences
# in every locale, which mbrtowc(3) and iconv(1) do not all do (see utf8.bats).
# Usage: utf8-validate.sh [FILE]
# Prints "ok N bytes" and exits 0, or "invalid at byte offset N: REASON" and exits 1; 2 is a usage error.
set -u
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ ! -r "$1" ]; }; then
  echo "usage: utf8-validate.sh [FILE]" >&2
  exit 2
fi
LC_ALL=C
export LC_ALL
# od -An -v -tu1 prints every byte as an unsigned decimal, 16 per line, with no address column (POSIX od).
od -An -v -tu1 "$@" | awk '
  function fail(why) {
    printf "invalid at byte offset %d: %s\n", off, why
    bad = 1
    exit 1
  }
  {
    for (i = 1; i <= NF; i++) {
      b = $i + 0
      if (need > 0) {
        # A continuation byte: the first one may be narrowed by the lead byte, the rest use 80..BF.
        if (b < lo || b > hi) fail("byte " b " is not a valid continuation here")
        need--
        lo = 128
        hi = 191
      } else if (b < 128) {
        # ASCII
      } else if (b >= 194 && b <= 223) { need = 1; lo = 128; hi = 191 }
      else if (b == 224) { need = 2; lo = 160; hi = 191 }
      else if (b >= 225 && b <= 236) { need = 2; lo = 128; hi = 191 }
      else if (b == 237) { need = 2; lo = 128; hi = 159 }
      else if (b >= 238 && b <= 239) { need = 2; lo = 128; hi = 191 }
      else if (b == 240) { need = 3; lo = 144; hi = 191 }
      else if (b >= 241 && b <= 243) { need = 3; lo = 128; hi = 191 }
      else if (b == 244) { need = 3; lo = 128; hi = 143 }
      else fail("byte " b " cannot start a sequence")
      off++
    }
  }
  END {
    if (!bad && need > 0) {
      printf "invalid at byte offset %d: truncated sequence at end of input\n", off
      bad = 1
    }
    if (bad) exit 1
    printf "ok %d bytes\n", off
  }
'
