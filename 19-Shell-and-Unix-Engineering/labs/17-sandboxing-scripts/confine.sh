#!/bin/sh
# confine.sh: the cooperative layer of a script sandbox.
# Runs an absolute-path command with a scrubbed environment, a fixed working directory, a closed
# stdin, no inherited descriptors 3-9, a restrictive umask, and optional resource limits.
# It bounds what the command inherits; it does not bound what the command can do (see the note).
# Usage: confine.sh [-f BLOCKS] SCRATCH CMD [ARG...]
#   -f BLOCKS  cap the size of any file the command writes (also disables core dumps)
#   CMD        must be an absolute path: no PATH search, and "name=value" cannot be mistaken
#              for an environment assignment by env
# Exit status: 125 when the confinement cannot be set up (fail closed), else the status of CMD.
fsize=
if [ "${1-}" = -f ]; then
  [ "$#" -ge 2 ] || exit 125
  fsize=$2
  shift 2
fi
if [ "$#" -lt 2 ]; then
  echo "usage: confine.sh [-f BLOCKS] SCRATCH CMD [ARG...]" >&2
  exit 125
fi
case $2 in
  /*) ;;
  *)
    echo "confine.sh: CMD must be an absolute path: $2" >&2
    exit 125
    ;;
esac
# A hostile CDPATH can make cd land somewhere else and print the directory.
unset CDPATH
cd -- "$1" || exit 125
scratch=$(pwd -P) || exit 125
shift
umask 077
if [ -n "$fsize" ]; then
  # Fail closed: a shell without ulimit (posh) must refuse, never run the command unlimited.
  # shellcheck disable=SC3045 # POSIX.1-2024 XCU ulimit defines -c and -f; ShellCheck still flags them
  ulimit -c 0 || exit 125
  # shellcheck disable=SC3045 # same as above
  ulimit -f "$fsize" || exit 125
fi
exec /usr/bin/env -i PATH=/usr/bin:/bin HOME="$scratch" TMPDIR="$scratch" LANG=C LC_ALL=C "$@" \
  0</dev/null 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-
