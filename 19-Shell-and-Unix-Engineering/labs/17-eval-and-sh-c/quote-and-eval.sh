#!/bin/sh
# quote-and-eval.sh: build shell code from data safely, and show what breaks when you do not.
# Usage: quote-and-eval.sh MODE [ARG...]
#   quote ARG...              print the ARGs as one POSIX-quoted, space-joined command string
#   eval-join ARG...          eval "$@": join with spaces and re-parse, exactly as ssh does (unsafe)
#   eval-quoted ARG...        quote every ARG first, then eval: each ARG stays one word
#   setvar NAME VALUE         validate NAME, then eval "NAME=\$2": VALUE is never re-parsed
#   setvar-unsafe NAME VALUE  validate NAME, then eval "NAME=VALUE": VALUE is re-parsed as code
set -u

# shquote WORD: print WORD as one single-quoted shell word that every POSIX shell parses
# back to the identical bytes. Each ' becomes '\'' (close the quote, an escaped quote, reopen).
shquote() {
  sq_in=$1
  sq_out=
  while :; do
    case $sq_in in
      *\'*)
        sq_out=$sq_out${sq_in%%\'*}\'\\\'\'
        sq_in=${sq_in#*\'}
        ;;
      *)
        sq_out=$sq_out$sq_in
        break
        ;;
    esac
  done
  printf "'%s'" "$sq_out"
}

# quote_all ARG...: print every ARG quoted, separated by single spaces, with no newline.
quote_all() {
  qa_sep=
  for qa_arg; do
    printf '%s' "$qa_sep"
    shquote "$qa_arg"
    qa_sep=' '
  done
}

# show ARG...: print the argument count, then each argument in angle brackets.
show() {
  printf '%s args:' "$#"
  if [ "$#" -gt 0 ]; then
    printf ' <%s>' "$@"
  fi
  printf '\n'
}

# is_name WORD: succeed if WORD is a POSIX name (XBD 3.216), the only thing safe to splice.
is_name() {
  case $1 in
    '' | [!a-zA-Z_]* | *[!a-zA-Z0-9_]*) return 1 ;;
    *) return 0 ;;
  esac
}

mode=${1-}
if [ "$#" -gt 0 ]; then
  shift
fi

case $mode in
  quote)
    quote_all ${1+"$@"}
    ;;
  eval-join)
    # shellcheck disable=SC2294 # joining and re-parsing the words is the defect this mode shows
    eval ${1+"$@"}
    ;;
  eval-quoted)
    code=$(quote_all ${1+"$@"})
    eval "$code"
    ;;
  setvar | setvar-unsafe)
    if ! is_name "$1"; then
      printf 'invalid name: %s\n' "$1" >&2
      exit 2
    fi
    if [ "$mode" = setvar ]; then
      eval "$1=\$2"
    else
      eval "$1=$2"
    fi
    value=
    eval "value=\$$1"
    printf '%s=%s\n' "$1" "$value"
    ;;
  *)
    printf 'usage: %s quote|eval-join|eval-quoted|setvar|setvar-unsafe [ARG...]\n' "$0" >&2
    exit 64
    ;;
esac
