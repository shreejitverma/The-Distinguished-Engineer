#!/bin/sh
# arg_cross_probe.sh: demonstrates how sudoers wildcard argument concatenation
# allows cross-argument traversal unless regular expressions are used.
#
# Usage:
#   arg_cross_probe.sh glob   PATTERN ARG1 [ARG2...]
#   arg_cross_probe.sh regex  PATTERN ARG1 [ARG2...]

set -eu

# zsh treats an unquoted parameter in a case pattern as a literal unless GLOB_SUBST is set; POSIX shells glob it.
[ -z "${ZSH_VERSION-}" ] || setopt GLOB_SUBST

mode="${1:-}"
pattern="${2:-}"

if [ "$#" -lt 2 ]; then
  printf 'Usage: %s <glob|regex> <pattern> [args...]\n' "$0" >&2
  exit 1
fi

shift 2

# Concatenate all arguments into a single space-separated string,
# exactly as sudo does before evaluating command arguments.
joined=""
for arg in "$@"; do
  if [ -z "$joined" ]; then
    joined="$arg"
  else
    joined="$joined $arg"
  fi
done

case "$mode" in
  glob)
    # Shell glob matching against concatenated string.
    # In sudoers, * matches across word boundaries and slashes.
    # shellcheck disable=SC2254 # pattern is intentionally an unquoted glob pattern
    case "$joined" in
      $pattern)
        printf 'MATCHED: concatenated "%s" matched glob "%s"\n' "$joined" "$pattern"
        exit 0
        ;;
      *)
        printf 'DENIED: concatenated "%s" did not match glob "%s"\n' "$joined" "$pattern"
        exit 1
        ;;
    esac
    ;;
  regex)
    # Strict regex matching using awk.
    # Sudoers regex patterns start with ^ and end with $.
    # shellcheck disable=SC2312 # intentional assignment of awk exit status
    if awk -v str="$joined" -v reg="$pattern" 'BEGIN { exit !(str ~ reg) }'; then
      printf 'MATCHED: concatenated "%s" matched regex "%s"\n' "$joined" "$pattern"
      exit 0
    else
      printf 'DENIED: concatenated "%s" did not match regex "%s"\n' "$joined" "$pattern"
      exit 1
    fi
    ;;
  *)
    printf 'Error: unknown mode "%s" (must be glob or regex)\n' "$mode" >&2
    exit 2
    ;;
esac
