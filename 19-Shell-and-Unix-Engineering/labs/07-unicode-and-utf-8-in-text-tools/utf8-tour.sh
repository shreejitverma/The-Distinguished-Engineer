#!/bin/sh
# utf8-tour.sh: the same UTF-8 text seen by byte tools and by a character-aware C library.
# Every line it prints follows from RFC 3629, POSIX.1-2024 or the C library, so it is identical
# on every conforming system; implementation differences between sed, awk, cut, tr and the
# shells live in utf8.bats, where each one is asserted per implementation.
# Non-ASCII bytes are built with printf octal escapes so this file stays ASCII.
# Usage: utf8-tour.sh PROBE   (PROBE is the compiled utf8probe; needs the C.UTF-8 locale)
# shellcheck disable=SC2312 # each helper ends in a pipeline whose last stage carries the status that matters
set -u
probe=${1:?usage: utf8-tour.sh PROBE}
LC_ALL=C.UTF-8
export LC_ALL

e_nfc=$(printf '\303\251')        # U+00E9 LATIN SMALL LETTER E WITH ACUTE
e_nfd=$(printf 'e\314\201')       # U+0065 U+0301: the canonically equivalent NFD form
euro=$(printf '\342\202\254')     # U+20AC EURO SIGN
grin=$(printf '\360\237\230\200') # U+1F600 GRINNING FACE
rlo=$(printf '\342\200\256')      # U+202E RIGHT-TO-LEFT OVERRIDE (Trojan Source)
bom=$(printf '\357\273\277')      # U+FEFF as a UTF-8 signature
nfc="caf$e_nfc"
nfd="caf$e_nfd"

show() { printf '%-26s %s\n' "$1" "$2"; }
hex() { od -An -tx1 | awk '{ for (i = 1; i <= NF; i++) printf "%s%s", (n++ ? " " : ""), $i } END { print "" }'; }
bytes() { printf %s "$1" | wc -c | tr -d ' '; }
# chars: how many characters mbrtowc(3) finds in a string, counted from the probe's decoding steps.
chars() { "$probe" decode C.UTF-8 "$(printf %s "$1" | od -An -v -tx1 | tr -d ' \n')" | awk '{ print NF }'; }

show 'bytes U+00E9' "$(bytes "$e_nfc")"
show 'bytes U+20AC' "$(bytes "$euro")"
show 'bytes U+1F600' "$(bytes "$grin")"
show 'bytes cafe (NFC)' "$(bytes "$nfc")"
show 'bytes cafe (NFD)' "$(bytes "$nfd")"
show 'chars cafe (NFC)' "$(chars "$nfc")"
show 'chars cafe (NFD)' "$(chars "$nfd")"
nfd_hits=$(printf '%s\n' "$nfd" | grep -c -F "$nfc")
show 'grep NFC in NFD text' "$nfd_hits"
show 'sort -u NFC and NFD' "$(printf '%s\n%s\n' "$nfc" "$nfd" | LC_ALL=C sort -u | wc -l | tr -d ' ')"
show 'BOM in first field' "$(printf '%sid,name\n' "$bom" | cut -d, -f1 | hex)"
show 'C sort = code points' "$(printf '%s\n' "$grin" "$euro" z "$e_nfc" | LC_ALL=C sort | hex)"
rlo_hits=$(printf 'ok\nx = 1 %s// y\n' "$rlo" | LC_ALL=C grep -c -F "$rlo")
show 'lines with U+202E' "$rlo_hits"
