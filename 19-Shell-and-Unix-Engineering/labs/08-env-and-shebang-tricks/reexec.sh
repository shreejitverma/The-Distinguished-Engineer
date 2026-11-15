#!/bin/sh
# reexec.sh: a portable /bin/sh entry point that re-executes itself, once, under a
# preferred interpreter found at run time, keeping every argument byte for byte.
# This is the trampoline configure scripts use (CONFIG_SHELL) to escape a weak /bin/sh.
# Usage: reexec.sh [ARG...]
#   REEXEC_WANT  interpreter to switch to (default: bash), a name on PATH or a path.
#   REEXEC_DONE  set by the trampoline before it execs; when already set it never
#                re-executes again, which is what stops an infinite exec loop.
# When it does not switch, it prints "native reexec=VALUE: <arg>..." itself.
want=${REEXEC_WANT:-bash}
if [ -z "${REEXEC_DONE-}" ] && command -v "$want" >/dev/null 2>&1; then
  REEXEC_DONE=1
  export REEXEC_DONE
  # Plain "$@", not the V7-era ${1+"$@"}: the autoconf manual reports ksh93 93t+
  # dropping a trailing empty argument from ${1+"$@"}.
  exec "$want" "$0" "$@"
fi
printf 'native reexec=%s:' "${REEXEC_DONE-unset}"
if [ "$#" -gt 0 ]; then
  printf ' <%s>' "$@"
fi
printf '\n'
