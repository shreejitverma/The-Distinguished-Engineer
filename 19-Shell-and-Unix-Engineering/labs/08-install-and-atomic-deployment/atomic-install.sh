#!/bin/sh
# atomic-install.sh MODE SRC DEST: replace DEST with a copy of SRC that has mode MODE,
# so that every reader sees either the old DEST or the complete new one, never a missing
# or partial file. install(1) builds the file under a private staging directory next to
# DEST (same file system, so the final mv is one rename(2)), with its mode set before
# the file has a public name; mv then renames it over DEST.
# Durability is not covered: nothing here calls fsync (see the note).
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: atomic-install.sh MODE SRC DEST" >&2
  exit 2
fi
mode=$1 src=$2 dest=$3

case $dest in
  */)
    echo "atomic-install.sh: $dest: names a directory" >&2
    exit 1
    ;;
  */*) dir=${dest%/*} ;;
  *) dir=. ;;
esac
dir=${dir:-/} # "/name" has an empty directory part
if [ -d "$dest" ]; then
  # mv would move the new file INTO the directory instead of replacing it
  echo "atomic-install.sh: $dest: is a directory" >&2
  exit 1
fi

stage=$(mktemp -d "$dir/.stage.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
trap 'exit 1' HUP INT TERM

install -m "$mode" -- "$src" "$stage/file"
mv -f -- "$stage/file" "$dest"
