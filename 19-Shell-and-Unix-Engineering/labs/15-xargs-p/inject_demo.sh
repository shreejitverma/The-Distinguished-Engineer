#!/bin/sh
# inject_demo: show what "xargs -I{}" does when {} is pasted into a "sh -c" script string.
# Usage: inject_demo.sh unsafe|safe DIR
# The single NUL-delimited item is 'x;touch PWNED'; the cwd is DIR.
# unsafe: sh -c ': {}' -> the item is shell code and creates DIR/PWNED.
# safe:   sh -c ': "$1"' _ {} -> the item is data in $1 and nothing runs.
mode=$1
dir=$2
cd "$dir" || exit 2
x=${XARGS:-xargs}
# shellcheck disable=SC2016 # the $1 belongs to the inner sh -c, which is the point of the demo
case $mode in
  unsafe) printf '%s\0' 'x;touch PWNED' | "$x" -0 -P 2 -I{} sh -c ': {}' ;;
  safe) printf '%s\0' 'x;touch PWNED' | "$x" -0 -P 2 -I{} sh -c ': "$1"' _ {} ;;
  *) exit 2 ;;
esac
if [ -e PWNED ]; then echo PWNED; else echo clean; fi
