# shellcheck shell=bash
# stacktrace.bash: source this file, then call die MESSAGE [STATUS] to print a stack trace.
# Works in bash 3.2 and later; it reads FUNCNAME, BASH_SOURCE and BASH_LINENO directly
# instead of calling caller in a loop, which would fork once per frame.

# stacktrace [SKIP]: print one "  at FUNCTION (FILE:LINE)" line per frame to stderr,
# innermost first, skipping this function and SKIP more frames.
# Frame i runs FUNCNAME[i], defined in BASH_SOURCE[i], now at line BASH_LINENO[i-1].
stacktrace() {
  local i
  for ((i = ${1:-0} + 1; i < ${#FUNCNAME[@]}; i++)); do
    printf '  at %s (%s:%s)\n' "${FUNCNAME[i]}" "${BASH_SOURCE[i]##*/}" "${BASH_LINENO[i - 1]}" >&2
  done
}

# die MESSAGE [STATUS]: print "SCRIPT: fatal: MESSAGE" and the stack to stderr, then exit.
die() {
  local top=$((${#BASH_SOURCE[@]} - 1))
  printf '%s: fatal: %s\n' "${BASH_SOURCE[top]##*/}" "$1" >&2
  stacktrace 1
  exit "${2:-1}"
}
