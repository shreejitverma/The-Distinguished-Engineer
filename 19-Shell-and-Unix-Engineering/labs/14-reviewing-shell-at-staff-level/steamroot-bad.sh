#!/bin/sh
# steamroot-bad.sh: a reduction of the 2015 Steam for Linux launcher bug, not Valve's code.
# It computes the script's own directory from $0 and prints the command a reset would run.
# noglob keeps the printed glob unexpanded, so nothing here lists or touches the real root.
set -u
set -o noglob
STEAMROOT="$(cd "${0%/*}" && echo "$PWD")"
printf 'rm -rf %s\n' "$STEAMROOT/"*
