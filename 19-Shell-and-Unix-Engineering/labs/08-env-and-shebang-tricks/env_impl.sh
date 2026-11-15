#!/bin/sh
# env_impl.sh: name the env(1) implementation behind a command, so tests branch on the
# implementation (it decides -S, -C, -a, and signal options), not on the shell under test.
# Usage: env_impl.sh [ENV_COMMAND]   (default: /usr/bin/env)
# Prints one line: IMPL VERSION, where IMPL is gnu, uutils, busybox, bsd, or unknown,
# and VERSION is the reported version for gnu and uutils, else n/a.
e=${1:-/usr/bin/env}
out=$("$e" --version 2>&1 </dev/null) || true
path=$(command -v "$e") || path=$e
real=$(readlink -f "$path" 2>/dev/null) || real=$path
case $out in
  *"GNU coreutils"*) impl=gnu ;;
  *uutils*) impl=uutils ;;
  *BusyBox*) impl=busybox ;;
  *"illegal option"*) impl=bsd ;;
  *)
    case $real in
      */busybox) impl=busybox ;;
      *) impl=unknown ;;
    esac
    ;;
esac
version=n/a
case $impl in
  gnu | uutils)
    first=${out%%"
"*}
    version=${first##* }
    ;;
  *) ;;
esac
printf '%s %s\n' "$impl" "$version"
