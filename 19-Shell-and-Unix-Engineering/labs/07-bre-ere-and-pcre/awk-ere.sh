#!/bin/sh
# awk-ere.sh: show that awk patterns are EREs that also decode C escapes inside brackets.
# Usage: awk-ere.sh AWK [ARG...]   (for example: awk-ere.sh awk, or awk-ere.sh busybox awk)
# Input lines: a, aa, a+, n, and a lone backslash.
# Prints "ere:LINE" for each line /^a+$/ selects (+ is an operator, so a and aa),
# and "bracket:LINE" for each line /^[\n]$/ selects (none: [\n] is a newline here, not n or \).
# shellcheck disable=SC2016 # $0 belongs to the awk program, not the shell
printf 'a\naa\na+\nn\n\\\n' | "$@" '
  /^a+$/ { print "ere:" $0 }
  /^[\n]$/ { print "bracket:" $0 }
'
