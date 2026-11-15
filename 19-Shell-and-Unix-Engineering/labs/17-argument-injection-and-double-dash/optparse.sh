#!/bin/sh
# optparse: show how the shell's getopts splits ARGS into options and operands.
# The option string is "ab:": -a takes no argument, -b takes one.
# Output is one line: "opts: a b=VAL | operands: <x> <y>".
# Usage: optparse.sh [ARG...]
opts=''
OPTIND=1
while getopts ab: opt; do
  case $opt in
    a) opts="$opts a" ;;
    b) opts="$opts b=$OPTARG" ;;
    *) opts="$opts error" ;;
  esac
done
shift $((OPTIND - 1))
printf 'opts:%s | operands:' "$opts"
for arg in "$@"; do
  printf ' <%s>' "$arg"
done
printf '\n'
