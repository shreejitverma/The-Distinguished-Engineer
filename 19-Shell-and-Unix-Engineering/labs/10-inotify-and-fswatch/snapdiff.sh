#!/bin/sh
# snapdiff.sh - the portable fallback when no notification API is usable (NFS, SMB,
# FUSE, a container bind mount from another kernel, or a shell with no watcher):
# snapshot a tree with POSIX find and cksum, then diff two snapshots.
#
#   snapdiff.sh snap DIR        print one "CRC SIZE PATH" line per regular file, sorted
#   snapdiff.sh diff OLD NEW    print "A PATH", "D PATH", or "M PATH" per change, sorted
#
# The checksum, not the timestamp, decides "modified", so a same-size rewrite inside one
# timestamp tick is still caught. Exit status: 0 success, 2 usage, 3 a file name contains
# a newline (the line-oriented snapshot cannot represent it, so refuse instead of lying).
set -eu

nl='
'

usage() {
  printf 'usage: snapdiff.sh snap DIR | diff OLD NEW\n' >&2
  exit 2
}

snap() {
  dir=$1
  [ -d "$dir" ] || usage
  bad=$(find "$dir" -name "*$nl*" -exec printf x \;)
  if [ -n "$bad" ]; then
    printf 'snapdiff: a file name under %s contains a newline\n' "$dir" >&2
    exit 3
  fi
  find "$dir" -type f -exec cksum {} + | LC_ALL=C sort -k 3
}

diff_snaps() {
  # Key = everything after the second field (the path, spaces included).
  # FILENAME, not FNR == NR, picks the old snapshot: FNR == NR is true for every line of
  # NEW when OLD is empty (an empty directory), which would lose every addition.
  LC_ALL=C awk '
    function key(line) {
      sub(/^[^ ]+ [^ ]+ /, "", line)
      return line
    }
    function sig(line) {
      split(line, f, " ")
      return f[1] " " f[2]
    }
    FILENAME == ARGV[1] { old[key($0)] = sig($0); next }
    {
      k = key($0)
      if (!(k in old)) print "A " k
      else if (old[k] != sig($0)) print "M " k
      seen[k] = 1
    }
    END { for (k in old) if (!(k in seen)) print "D " k }
  ' "$1" "$2" | LC_ALL=C sort -k 2
}

[ "$#" -ge 1 ] || usage
case $1 in
  snap)
    [ "$#" -eq 2 ] || usage
    snap "$2"
    ;;
  diff)
    [ "$#" -eq 3 ] || usage
    diff_snaps "$2" "$3"
    ;;
  *) usage ;;
esac
