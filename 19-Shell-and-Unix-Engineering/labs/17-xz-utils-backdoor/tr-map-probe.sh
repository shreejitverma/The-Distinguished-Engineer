#!/bin/sh
# Apply a tr substitution map (string1 -> \0-\377) to all 256 byte values and report what came out.
# usage: tr-map-probe.sh MAP
# Prints "distinct=N first=A B C D E F" computed from tr's real output.
LC_ALL=C
export LC_ALL
if [ "$#" -ne 1 ]; then
  echo "usage: tr-map-probe.sh MAP" >&2
  exit 2
fi
vals=$(
  i=0
  while [ "$i" -lt 256 ]; do
    # shellcheck disable=SC2059 # the octal escape is built on purpose
    printf "\\$(printf '%03o' "$i")"
    i=$((i + 1))
  done | tr "$1" "\0-\377" | od -An -v -tu1 | tr -s ' ' '\n' | grep .
)
distinct=$(printf '%s\n' "$vals" | sort -un | grep -c .)
first=$(printf '%s\n' "$vals" | head -n 6 | tr '\n' ' ')
echo "distinct=$distinct first=${first% }"
