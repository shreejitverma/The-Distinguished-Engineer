# shellcheck shell=bash
# outparam_lib.sh: two ways to return a value into a variable the caller names.
# Sourced by outparam.sh only after it has proved that `typeset -n` works,
# so shells without namerefs never parse this file.
# The `function NAME {` form is deliberate: it is the form in which ksh93 gives
# typeset static scope, and bash and mksh treat it exactly like NAME().

# naive_max OUT N...: store the largest N in the variable named OUT.
# Bug: OUT is resolved when it is used, so a caller variable that shares a
# name with one of these locals (best, n, out) is shadowed and never written.
function naive_max {
  typeset -n out=$1
  typeset best=$2 n
  shift 2
  for n in "$@"; do
    if ((n > best)); then
      best=$n
    fi
  done
  # shellcheck disable=SC2034 # out is a nameref: this assigns the caller's variable
  out=$best
}

# guarded_max OUT N...: the same, but OUT must be a plain identifier outside the
# function's private prefix, so neither shadowing nor subscript injection can happen.
function guarded_max {
  case $1 in
    '' | [0123456789]* | *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_]*)
      printf 'guarded_max: not an identifier: %s\n' "$1" >&2
      return 2
      ;;
    _gm_*)
      printf 'guarded_max: reserved name: %s\n' "$1" >&2
      return 2
      ;;
    *) ;;
  esac
  typeset -n _gm_out=$1
  typeset _gm_best=$2 _gm_n
  shift 2
  for _gm_n in "$@"; do
    if ((_gm_n > _gm_best)); then
      _gm_best=$_gm_n
    fi
  done
  # shellcheck disable=SC2034 # _gm_out is a nameref: this assigns the caller's variable
  _gm_out=$_gm_best
}
