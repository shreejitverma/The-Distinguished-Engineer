#!/bin/sh
# shellcheck disable=SC3040 # errtrace is reached only after the script has checked that it runs under bash
# err-trace.sh: fail three calls deep under errexit plus errtrace and print a stack trace.
# Prints "err-trace.sh: needs bash" and exits 2 under any other shell.
# Usage: err-trace.sh [STATUS]   (default 3; the script exits with STATUS)
if [ -z "${BASH_VERSION-}" ]; then
  echo "err-trace.sh: needs bash" >&2
  exit 2
fi
set -o errexit -o errtrace -o nounset
case $0 in
  */*) here=${0%/*} ;;
  *) here=. ;;
esac
. "$here/stacktrace.bash"

fetch() {
  /bin/sh -c "exit $1"
  echo "fetch: not reached"
}

process() {
  fetch "$1"
}

run() {
  process "$1"
}

run "${1:-3}"
echo "top: not reached"
