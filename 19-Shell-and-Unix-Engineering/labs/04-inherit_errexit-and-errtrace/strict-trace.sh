#!/usr/bin/env bash
# strict-trace.sh MODE: a bash strict-mode prelude whose ERR trap prints a stack trace.
# MODE is nested (a failure two functions deep), comsub (the same failure inside an
# assignment's command substitution), or masked (inside a substitution whose status is lost).
# Every line the trap prints goes to stderr, so it never leaks into a substitution's value.

# Only POSIX syntax before this guard: other shells must reach it and stop here.
case ${BASH_VERSION-} in
  "" | [123].* | 4.[0-3].*)
    echo "strict-trace.sh: needs bash 4.4 or later" >&2
    exit 2
    ;;
  *) ;;
esac

set -Eeuo pipefail
shopt -s inherit_errexit

on_err() {
  local status=$? i=1
  printf 'ERR status=%s subshell=%s command=%s\n' \
    "$status" "$BASH_SUBSHELL" "$BASH_COMMAND" >&2
  while [ "$i" -lt "${#FUNCNAME[@]}" ]; do
    printf '  at %s line %s\n' "${FUNCNAME[i]}" "${BASH_LINENO[i - 1]}" >&2
    i=$((i + 1))
  done
  return "$status"
}
trap on_err ERR

read_config() {
  false
  echo "unreachable: read_config continued"
}

load() {
  read_config
}

# shellcheck disable=SC2312 # the masked mode loses the substitution's status on purpose
case ${1-} in
  nested)
    load
    ;;
  comsub)
    value=$(load)
    echo "unreachable: value=$value"
    ;;
  masked)
    echo "masked: [$(load)]"
    ;;
  *)
    echo "usage: strict-trace.sh nested|comsub|masked" >&2
    exit 64
    ;;
esac
echo "end of script"
