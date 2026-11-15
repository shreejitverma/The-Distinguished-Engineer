#!/bin/sh
# timeout_impl.sh: name the timeout(1) implementation that TIMEOUT (default: timeout on PATH) runs.
# Prints one line: "gnu MAJOR.MINOR", "uutils", "busybox", "bsd" (FreeBSD and macOS), or "unknown".
# The answer comes from what the binary prints, never from the platform name.
T=${TIMEOUT:-timeout}
out=$("$T" --version 2>&1 </dev/null)
case $out in
  *"GNU coreutils"*)
    ver=$(printf '%s\n' "$out" | sed -n '1s/^.* \([0-9][0-9]*\.[0-9][0-9]*\).*$/\1/p')
    printf 'gnu %s\n' "$ver"
    ;;
  *uutils*) echo uutils ;;
  *BusyBox*) echo busybox ;;
  *"[-f | --foreground]"*) echo bsd ;;
  *) echo unknown ;;
esac
