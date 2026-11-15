#!/bin/sh
# passthrough.sh MODE CMD [ARG...]: run CMD the three ways wrappers usually do.
#   exec    replace this shell with CMD, so the caller sees exactly how CMD ended
#   status  run CMD, then exit "$rc"; a signal death becomes a number
#   last    run CMD as the final command and let the shell exit on its own
mode=$1
shift
case $mode in
  exec) exec "$@" ;;
  status)
    "$@"
    rc=$?
    exit "$rc"
    ;;
  last) "$@" ;;
  *)
    printf 'usage: passthrough.sh exec|status|last CMD [ARG...]\n' >&2
    exit 2
    ;;
esac
