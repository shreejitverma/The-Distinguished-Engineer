#!/bin/sh
# Compare a release tarball tree with the git tree it claims to come from.
# usage: distdiff.sh GITDIR TARDIR [REFDIR]
# Prints "EXTRA path status" for each file only in the tarball (status: same-as-reference, DIFFERS, or
# no-reference) and "CHANGED path" for each file in both whose bytes differ. Exits 1 if any EXTRA is DIFFERS.
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "usage: distdiff.sh GITDIR TARDIR [REFDIR]" >&2
  exit 2
fi
git=$1
tar=$2
ref=${3-}
files=$(cd "$tar" && find . -type f | sort | sed 's|^\./||')
report=$(
  printf '%s\n' "$files" | while IFS= read -r f; do
    if [ -f "$git/$f" ]; then
      cmp -s "$git/$f" "$tar/$f" || echo "CHANGED $f"
    elif [ -n "$ref" ] && [ -f "$ref/$f" ]; then
      if cmp -s "$ref/$f" "$tar/$f"; then
        echo "EXTRA $f same-as-reference"
      else
        echo "EXTRA $f DIFFERS"
      fi
    else
      echo "EXTRA $f no-reference"
    fi
  done
)
printf '%s\n' "$report"
case $report in
  *" DIFFERS"*) exit 1 ;;
  *) exit 0 ;;
esac
