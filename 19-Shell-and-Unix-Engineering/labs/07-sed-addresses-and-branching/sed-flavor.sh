#!/bin/sh
# sed-flavor.sh [SED [ARG...]]: print which sed implementation a command runs:
# gnu, bsd (FreeBSD and macOS), busybox, uutils, or unknown.
# The shell running this script never changes which sed runs, so the lab's
# tests branch on this flavor, not on the shell family.
if [ "$#" -eq 0 ]; then
  set -- sed
fi
out=$("$@" --version 2>&1 </dev/null) || true
case $out in
  *"not GNU sed"*) echo busybox ;; # BusyBox sed --version prints "This is not GNU sed version 4.0"
  *"(uutils)"*) echo uutils ;;
  *"GNU sed"*) echo gnu ;;
  *"usage: sed script"*) echo bsd ;; # FreeBSD and macOS reject --version with their usage text
  *) echo unknown ;;
esac
