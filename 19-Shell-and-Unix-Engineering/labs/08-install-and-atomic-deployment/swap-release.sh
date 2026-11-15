#!/bin/sh
# swap-release.sh BASE RELEASE: point BASE/current at BASE/releases/RELEASE in one
# rename(2), so a reader that resolves BASE/current sees the old release or the new one,
# never a missing link. The new link is built in a private staging directory under BASE
# and moved INTO BASE: in that form mv renames over BASE/current itself instead of
# following it into the old release, with no GNU -T or BSD -h needed.
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: swap-release.sh BASE RELEASE" >&2
  exit 2
fi
base=$1 release=$2

case $release in
  "" | */* | . | ..)
    echo "swap-release.sh: $release: not a release name" >&2
    exit 1
    ;;
  *) ;;
esac
if [ ! -d "$base/releases/$release" ]; then
  echo "swap-release.sh: $base/releases/$release: no such release" >&2
  exit 1
fi
if [ -e "$base/current" ] && [ ! -L "$base/current" ]; then
  echo "swap-release.sh: $base/current: exists and is not a symlink" >&2
  exit 1
fi

stage=$(mktemp -d "$base/.swap.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
trap 'exit 1' HUP INT TERM

# A relative target keeps the tree relocatable; it resolves against BASE once moved.
ln -s "releases/$release" "$stage/current"
mv -f -- "$stage/current" "$base/"
