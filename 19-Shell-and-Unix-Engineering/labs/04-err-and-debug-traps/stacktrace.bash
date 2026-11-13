# shellcheck shell=bash
# stacktrace.bash: an ERR handler that prints the failing command and a call stack, then exits.
# Source it after set -o errexit -o errtrace; it installs itself on ERR.

on_err() {
  local status=$1 cmd=$2 i
  # The first trap run is the innermost failure; exiting here stops the
  # errtrace double fire in every caller frame.
  trap - ERR
  printf 'error: status %s from [%s]\n' "$status" "$cmd" >&2
  for ((i = 1; i < ${#FUNCNAME[@]}; i++)); do
    printf '  at %s (%s:%s)\n' "${FUNCNAME[i]}" "${BASH_SOURCE[i]##*/}" "${BASH_LINENO[i - 1]}" >&2
  done
  exit "$status"
}

trap 'on_err "$?" "$BASH_COMMAND"' ERR
