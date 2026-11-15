#!/bin/sh
# rewrite: replace a file's contents the three ways scripts usually do it,
# so a test can check which ways keep the inode, and with it the file's
# extended attributes and ACL.
# Usage: rewrite.sh truncate|append|rename FILE TEXT
#   truncate  printf TEXT > FILE                      (same inode, O_TRUNC)
#   append    printf TEXT >> FILE                     (same inode, O_APPEND)
#   rename    write FILE.tmp.PID, then mv it over FILE (new inode)
# Exit status: that of the failing step, 0 on success, 2 on a usage error.
set -u
if [ "$#" -ne 3 ]; then
  printf 'usage: rewrite.sh truncate|append|rename FILE TEXT\n' >&2
  exit 2
fi
mode=$1 file=$2 text=$3
case $mode in
  truncate) printf '%s\n' "$text" >"$file" ;;
  append) printf '%s\n' "$text" >>"$file" ;;
  rename)
    tmp=$file.tmp.$$
    printf '%s\n' "$text" >"$tmp" || exit
    mv -f "$tmp" "$file"
    ;;
  *)
    printf 'rewrite.sh: unknown mode %s\n' "$mode" >&2
    exit 2
    ;;
esac
