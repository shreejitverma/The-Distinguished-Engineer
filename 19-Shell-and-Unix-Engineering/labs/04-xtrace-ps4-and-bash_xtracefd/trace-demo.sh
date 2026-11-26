#!/bin/sh
# trace-demo.sh: a production xtrace setup - the trace goes to its own log through
# BASH_XTRACEFD, every line names file, line and function, and a secret stays out of it.
# Prints "trace-demo.sh: needs bash" and exits 2 under any other shell.
# Usage: trace-demo.sh LOG   (LOG is appended to; stdout and stderr stay clean)
if [ -z "${BASH_VERSION-}" ]; then
  echo "trace-demo.sh: needs bash" >&2
  exit 2
fi
exec 3>>"$1"
BASH_XTRACEFD=3
PS4='+ ${BASH_SOURCE##*/}:${LINENO}:${FUNCNAME[0]:-main}: '
set -x

load_token() {
  { set +x; } 3>/dev/null 2>/dev/null
  # shellcheck disable=SC2034 # stands in for a credential a real script would pass to curl
  token=example-secret-not-real
  set -x
}

fetch() {
  load_token
  printf 'fetching %s\n' "$1"
}

fetch https://example.invalid/data
echo "warning: cache cold" >&2
