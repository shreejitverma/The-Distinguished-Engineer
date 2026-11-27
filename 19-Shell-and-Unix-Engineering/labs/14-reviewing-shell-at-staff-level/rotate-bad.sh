#!/bin/sh
# rotate-bad.sh DIR KEEP: delete all but the newest KEEP backups in DIR.
# The review exhibit: a plausible first draft with five findings that rotate.sh fixes.
# shellcheck disable=SC2086,SC2046,SC2012,SC2248 # intentionally flawed: this is the code under review
set -e
dir=$1
keep=$2
cd $dir
for f in $(ls -t backup-* | tail -n +$((keep + 1))); do
  rm $f
done
echo "rotated $dir"
