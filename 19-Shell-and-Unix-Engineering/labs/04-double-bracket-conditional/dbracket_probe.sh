#!/bin/sh
# dbracket_probe.sh: show what [[ is in the running shell, and where it differs from [.
# Usage: dbracket_probe.sh classify|contrast
#   classify  print kind=keyword, kind=builtin, kind=external, or kind=absent
#   contrast  run the same five checks through [ and through [[ and print both exit statuses
#
# Every [[ lives in a single-quoted eval string: dash and posh have no such keyword, mksh parses
# a whole file before running any of it, and shfmt would otherwise reformat the conditional.

set -u

# dbl CODE: evaluate CODE (a [[ command) in this shell and print its exit status.
dbl() {
  eval "$1" 2>/dev/null
  printf '%s' "$?"
}

# sgl ARG...: run [ with these arguments and print its exit status.
sgl() {
  # shellcheck disable=SC2244 # the arguments are a whole test expression, not one string
  [ "$@" ] 2>/dev/null
  printf '%s' "$?"
}

# row NAME SINGLE DOUBLE: print one line of the contrast table.
row() {
  printf '%-8s single=%s double=%s\n' "$1" "$2" "$3"
}

classify() {
  where=$(command -v '[[' 2>/dev/null) || where=
  case $where in
    '') kind=absent ;;
    */*) kind=external ;;
    *)
      # A keyword parses < as a string operator; a builtin leaves it to the shell, which tries
      # to open the right operand for reading and fails.
      order=$(dbl '[[ / < /nonexistent/dbracket-probe ]]')
      if [ "$order" = 0 ]; then
        kind=keyword
      else
        kind=builtin
      fi
      ;;
  esac
  printf 'kind=%s\n' "$kind"
}

contrast() {
  v='a b'
  # shellcheck disable=SC2086,SC2248 # unquoted on purpose: [ sees two words, [[ sees one
  s=$(sgl $v = 'a b')
  # shellcheck disable=SC2016 # the code is for eval, not for expansion here
  d=$(dbl '[[ $v == "a b" ]]')
  row split "$s" "$d"

  v=
  # shellcheck disable=SC2086,SC2248 # unquoted on purpose: the empty value vanishes from [
  s=$(sgl -n $v)
  # shellcheck disable=SC2016 # the code is for eval, not for expansion here
  d=$(dbl '[[ -n $v ]]')
  row empty "$s" "$d"

  s=$(sgl abc = 'a*')
  d=$(dbl '[[ abc == a* ]]')
  row pattern "$s" "$d"

  s=$(sgl 010 -eq 8)
  d=$(dbl '[[ 010 -eq 8 ]]')
  row octal "$s" "$d"

  s=$(sgl 1+1 -eq 2)
  d=$(dbl '[[ 1+1 -eq 2 ]]')
  row expr "$s" "$d"
}

case ${1-} in
  classify) classify ;;
  contrast) contrast ;;
  *)
    printf 'usage: %s classify|contrast\n' "$0" >&2
    exit 2
    ;;
esac
