#!/bin/sh
# rotate.sh [-n] DIR KEEP: delete all but the newest KEEP backups in DIR.
# Backups are named backup-YYYYMMDDTHHMMSSZ.tar, so name order is age order, oldest first.
# Rotation never trusts mtime: cp, rsync and restores rewrite it.
# -n prints "would remove PATH" for each file and removes nothing.
# Exit status: 0 done, 1 runtime failure, 2 usage error.
set -eu

usage() {
  echo "usage: rotate.sh [-n] DIR KEEP" >&2
  exit 2
}

dry=0
if [ "${1-}" = "-n" ]; then
  dry=1
  shift
fi
[ "$#" -eq 2 ] || usage
dir=$1
keep=$2
[ -n "$dir" ] || usage
# KEEP is a decimal count: digits only, no leading zero (bash and dash read 08 as bad octal), at most 9 digits.
case $keep in
  '' | *[!0-9]* | 0?*) usage ;;
  *) ;;
esac
[ "${#keep}" -le 9 ] || usage
if [ ! -d "$dir" ]; then
  echo "rotate.sh: not a directory: $dir" >&2
  exit 1
fi

# The glob is sorted by the shell; with no match it stays literal, so test the first word.
set -- "$dir"/backup-*.tar
if [ ! -e "$1" ] && [ ! -L "$1" ]; then
  exit 0
fi
excess=$(($# - keep))
while [ "$excess" -gt 0 ]; do
  if [ "$dry" -eq 1 ]; then
    printf 'would remove %s\n' "$1"
  else
    rm -f -- "$1"
  fi
  shift
  excess=$((excess - 1))
done
