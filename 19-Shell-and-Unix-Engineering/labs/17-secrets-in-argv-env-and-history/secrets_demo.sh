#!/bin/sh
# secrets_demo.sh: exercises command-line, environment, stdin, and heredoc secret passing.
# Usage:
#   secrets_demo.sh argv PROBE SECRET
#   secrets_demo.sh stdin PROBE SECRET
#   secrets_demo.sh env_leak PROBE SECRET
#   secrets_demo.sh env_scrub PROBE SECRET
#   secrets_demo.sh heredoc_type PROBE

set -eu

mode="${1:-}"
probe="${2:-}"
secret="${3:-}"

case "$mode" in
  argv)
    "$probe" argv_check "$secret" --token="$secret"
    ;;
  stdin)
    printf '%s' "$secret" | "$probe" argv_check "$secret"
    printf '%s' "$secret" | "$probe" stdin_read
    ;;
  env_leak)
    SECRET_ENV="$secret"
    export SECRET_ENV
    "$probe" env_check SECRET_ENV
    ;;
  env_scrub)
    SECRET_ENV="$secret"
    export SECRET_ENV
    env -i PATH="$PATH" "$probe" env_check SECRET_ENV
    ;;
  heredoc_type)
    "$probe" stdin_type <<EOF
secret
EOF
    ;;
  *)
    printf 'unknown mode: %s\n' "$mode" >&2
    exit 1
    ;;
esac
