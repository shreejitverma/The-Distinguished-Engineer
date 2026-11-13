#!/usr/bin/env bash
# requote.sh: print one command line that re-creates its arguments exactly when a shell parses it,
# using the bash 4.4+ ${*@Q} transformation (for ssh, su -c, or a generated script).
# Usage: requote.sh COMMAND [ARG...]
# Example: requote.sh printf '<%s>' 'a b'  ->  'printf' '<%s>' 'a b'
# Exit status 2 when the shell running it has no ${*@Q}; the check is feature detection, not a version test.
# A word with a non-printable byte comes out as $'...', and an ESC byte as \E, so the shell that
# parses the output must understand bash's $'...' escapes (bash, zsh, ksh93, mksh; not dash 0.5.12 or posh).

# The probe runs in a subshell: bash 5.2+ exits on a bad operator, and other shells reject the syntax.
if ! (eval 'set -- probe; : "${*@Q}"') 2>/dev/null; then
  # shellcheck disable=SC2016 # the message names the expansion literally
  printf 'requote.sh: this shell has no ${*@Q} (BASH_VERSION=%s)\n' "${BASH_VERSION:-unset}" >&2
  exit 2
fi
# eval keeps the bash-only syntax away from shells that parse the whole file before running it.
eval 'printf "%s\n" "${*@Q}"'
