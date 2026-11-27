#!/bin/sh
# inventory: print a small report that mixes stable fields with volatile ones.
# Volatile: the timestamp, the process id, the working directory, the environment.
# Usage: inventory.sh DIR
name=${0##*/}
if [ "$#" -ne 1 ]; then
  printf 'usage: %s DIR\n' "$name" >&2
  exit 2
fi
if ! cd "$1" 2>/dev/null; then
  printf '%s: cannot enter %s\n' "$name" "$1" >&2
  exit 1
fi
printf 'generated %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'pid=%d\n' "$$"
printf 'workdir=%s\n' "$PWD"
printf 'lc_all=%s tz=%s leak=%s\n' "${LC_ALL-unset}" "${TZ-unset}" "${GOLDEN_LEAK-unset}"
set -- *
printf 'order:'
for f in "$@"; do
  printf ' %s' "$f"
done
printf '\ncount=%d\n' "$#"
