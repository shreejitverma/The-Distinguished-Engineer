#!/bin/sh
# xargs_flavor: print which implementation the xargs command is.
# Usage: xargs_flavor.sh        (inspects "$XARGS", default xargs)
# Prints one of: gnu, busybox, uutils, bsd (FreeBSD and macOS), unknown.
x=${XARGS:-xargs}
out=$("$x" --version 2>&1 </dev/null) || true
case $out in
  *"GNU findutils"*) echo gnu ;;
  *uutils*) echo uutils ;;
  *BusyBox*) echo busybox ;;
  *"usage: xargs"*) echo bsd ;;
  *) echo unknown ;;
esac
