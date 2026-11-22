#!/bin/sh
# collect: run a bash script with kcov-style line tracing and keep the trace in a file.
# Usage: collect.sh ps4|debug TRACEFILE SCRIPT [ARG...]
# The traced shell is ${TRACE_BASH:-bash}; its exit status is this script's exit status.
method=$1
trace=$2
shift 2
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || exit 2
case $method in
  ps4) hook=$here/hook-ps4.sh ;;
  debug) hook=$here/hook-debug.sh ;;
  *)
    echo "collect: unknown method $method" >&2
    exit 2
    ;;
esac
: >"$trace" || exit 2
BASH_ENV=$hook BASH_XTRACEFD=7 KCOV_BASH_XTRACEFD=7 "${TRACE_BASH:-bash}" "$@" 7>>"$trace"
