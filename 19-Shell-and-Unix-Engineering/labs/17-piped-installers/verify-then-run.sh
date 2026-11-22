#!/bin/sh
# verify-then-run.sh: the safe replacement for `curl URL | sh`: fetch to a file, check a pinned
# SHA-256, and only then run it.
# Usage: verify-then-run.sh SOURCE_FILE EXPECTED_SHA256
# SOURCE_FILE stands in for the downloaded body; a real script would call `curl -fsSL -o FILE URL`
# and test curl's exit status before looking at the file.
# Exit status: 0 the installer ran and succeeded, 1 digest mismatch (nothing ran), 69 no SHA-256 tool.
set -eu
src=$1
want=$2
here=$(dirname -- "$0")
work=$(mktemp -d "${TMPDIR:-/tmp}/verify-then-run.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
cp -- "$src" "$work/install.sh"
got=$("$here/sha256-of.sh" "$work/install.sh") || exit 69
if [ "$got" != "$want" ]; then
  echo "refusing to run: sha256 $got does not match pinned $want" >&2
  exit 1
fi
sh "$work/install.sh"
