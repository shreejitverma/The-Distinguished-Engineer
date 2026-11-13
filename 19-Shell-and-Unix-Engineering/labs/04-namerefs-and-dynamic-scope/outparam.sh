#!/bin/sh
# outparam.sh MODE NAME: store max(3, 9, 4) in the variable called NAME, then report it.
#   naive     typeset -n with unprefixed locals (shadowing bug, no input check)
#   guarded   typeset -n behind an identifier check and a private local prefix
#   portable  eval behind the same check; runs in every POSIX shell
# Prints "status=N" and, when NAME is an identifier, "NAME=value" ("UNSET" if unset).
# Exits 3 with "namerefs: unsupported" when MODE needs typeset -n and the shell lacks it.

usage() {
  printf 'usage: outparam.sh naive|guarded|portable NAME\n' >&2
  exit 64
}

# is_identifier WORD: succeed when WORD is a shell variable name (locale-independent).
is_identifier() {
  case $1 in
    '' | [0123456789]* | *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_]*) return 1 ;;
    *) return 0 ;;
  esac
}

# portable_max OUT N...: the eval version; every local is a prefixed global.
portable_max() {
  if ! is_identifier "$1"; then
    printf 'portable_max: not an identifier: %s\n' "$1" >&2
    return 2
  fi
  case $1 in
    portable_max_*)
      printf 'portable_max: reserved name: %s\n' "$1" >&2
      return 2
      ;;
    *) ;;
  esac
  portable_max_out=$1
  portable_max_best=$2
  shift 2
  for portable_max_n in "$@"; do
    if [ "$portable_max_n" -gt "$portable_max_best" ]; then
      portable_max_best=$portable_max_n
    fi
  done
  # Safe only because portable_max_out was checked to be an identifier above.
  eval "$portable_max_out=\$portable_max_best"
}

[ "$#" -eq 2 ] || usage
mode=$1
name=$2
lib=$(dirname -- "$0")/outparam_lib.sh

case $mode in
  naive | guarded)
    # shellcheck disable=SC2034,SC3044 # a feature test: is the non-POSIX typeset -n accepted?
    if ! (typeset -n lab_ref=lab_target) >/dev/null 2>&1; then
      printf 'namerefs: unsupported\n'
      exit 3
    fi
    # shellcheck source=outparam_lib.sh
    . "$lib"
    ;;
  portable) ;;
  *) usage ;;
esac

if is_identifier "$name"; then
  eval "$name=preset"
fi
"${mode}_max" "$name" 3 9 4
printf 'status=%s\n' "$?"
if is_identifier "$name"; then
  eval "printf '%s=%s\n' \"\$name\" \"\${$name-UNSET}\""
fi
