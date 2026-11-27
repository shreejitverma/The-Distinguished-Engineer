#!/bin/sh
# dropper.sh - reference wrapper simulating privilege-dropping boundary hygiene:
# environment scrubbing, descriptor cloexec enforcement, and shell privileged mode probing.
#
# Usage:
#   dropper.sh scrub COMMAND [ARG...]
#   dropper.sh privcheck SHELL_CMD PAYLOAD_FILE
#   dropper.sh fdleak PROBE_BIN
#   dropper.sh fdclose PROBE_BIN

set -eu

mode="${1:-}"
shift || true

case "$mode" in
  scrub)
    if [ "$#" -lt 1 ]; then
      printf 'usage: dropper.sh scrub COMMAND [ARG...]\n' >&2
      exit 2
    fi
    safe_path="/usr/bin:/bin"
    safe_user="$(id -u -n 2>/dev/null || echo "nobody")"
    # Execute under an empty environment, preserving only explicitly safe variables
    exec env -i \
      PATH="$safe_path" \
      USER="$safe_user" \
      HOME="/tmp" \
      "$@"
    ;;

  privcheck)
    if [ "$#" -lt 2 ]; then
      printf 'usage: dropper.sh privcheck SHELL_CMD PAYLOAD_FILE\n' >&2
      exit 2
    fi
    target_shell="$1"
    payload="$2"

    # Run shell without -p
    out_normal=$(BASH_ENV="$payload" ENV="$payload" $target_shell -c 'printf "ran\n"' 2>&1)

    # Run shell with -p
    out_priv=$(BASH_ENV="$payload" ENV="$payload" $target_shell -p -c 'printf "ran\n"' 2>&1)

    printf 'normal: %s\n' "$out_normal"
    printf 'priv: %s\n' "$out_priv"
    ;;

  fdleak)
    if [ "$#" -lt 1 ]; then
      printf 'usage: dropper.sh fdleak PROBE_BIN\n' >&2
      exit 2
    fi
    probe="$1"
    # Open /dev/null on fd 3 without O_CLOEXEC, then exec probe
    exec 3</dev/null
    exec "$probe" fdcheck 3
    ;;

  fdclose)
    if [ "$#" -lt 1 ]; then
      printf 'usage: dropper.sh fdclose PROBE_BIN\n' >&2
      exit 2
    fi
    probe="$1"
    # Open /dev/null on fd 3
    exec 3</dev/null
    # Explicitly close fd 3 to simulate secure boundary hygiene
    exec 3<&-
    exec "$probe" fdcheck 3
    ;;

  *)
    printf 'dropper: unknown mode %s\n' "$mode" >&2
    exit 2
    ;;
esac
