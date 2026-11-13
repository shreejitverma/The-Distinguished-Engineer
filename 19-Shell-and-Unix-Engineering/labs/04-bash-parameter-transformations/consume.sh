#!/bin/sh
# consume.sh FILE: source FILE, which assigns y from text that bash's ${x@Q} wrote,
# then print the bytes of y in hex on one line, so any shell's decoding can be compared.
# Usage: consume.sh /path/to/fixtures/esc.q
# Example: consume.sh "$PWD/fixtures/newline.q"  ->  610a62
# shellcheck source=/dev/null # the fixture is chosen by the caller
. "$1"
# shellcheck disable=SC2154 # y is assigned by the sourced fixture
printf '%s' "$y" | od -An -tx1 | tr -d ' \n'
printf '\n'
