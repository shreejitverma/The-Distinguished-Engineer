#!/bin/sh
# batch_probe.sh: probe argument batching, delimiter parsing, and exit semantics under xargs.

set -eu

mode="${1:-}"
shift || true

case "$mode" in
  count)
    printf '%d args:' "$#"
    for arg in "$@"; do
      printf ' <%s>' "$arg"
    done
    printf '\n'
    ;;
  abort_on_target)
    target="${1:-}"
    shift || true
    for arg in "$@"; do
      if [ "$arg" = "$target" ]; then
        printf 'aborting on %s\n' "$arg" >&2
        exit 255
      fi
    done
    printf 'processed %d items\n' "$#"
    ;;
  *)
    printf 'Usage: %s {count|abort_on_target} [args...]\n' "$0" >&2
    exit 2
    ;;
esac
