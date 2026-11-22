#!/bin/sh
# translate.sh SPEC OUT: turn a spec written in a small ShellSpec-like DSL into a plain shell script.
# ShellSpec does the same with libexec/shellspec-translate.sh: a line-oriented pass over the spec that
# emits shell, which a separate shell process then runs. This is a simplified model, not that code.
#
# DSL (one statement per line, leading blanks ignored):
#   Include FILE          source FILE from the directory in $MS_LIB
#   Describe 'text'       group; its text prefixes the description of every example inside
#   It 'text'             one example; the lines up to its End are the example body
#   End                   close the innermost Describe or It
#   Data 'text'           stdin for the evaluation, one line
#   When call|run CMD...  the evaluation (see minispec.sh)
#   The ... should ...    an expectation (see minispec.sh)
# Any other line is copied through unchanged.

set -eu

[ $# -eq 2 ] || {
  printf 'usage: translate.sh SPEC OUT\n' >&2
  exit 2
}
spec=$1
out=$2

TAB=$(printf '\t.')
TAB=${TAB%.}

# quote WORD: print WORD single-quoted, so that the generated script holds the text literally.
quote() {
  q_in=$1
  q_out=''
  q_esc="'\\''"
  while :; do
    case $q_in in
      *\'*)
        q_out=$q_out${q_in%%\'*}$q_esc
        q_in=${q_in#*\'}
        ;;
      *) break ;;
    esac
  done
  printf "'%s'" "$q_out$q_in"
}

depth=0
prefix=''
func_no=0
lineno=0

{
  # shellcheck disable=SC2016 # the generated script must hold a literal $MS_LIB
  printf '. "$MS_LIB/minispec.sh"\n'
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    while :; do
      case $line in
        " "* | "$TAB"*) line=${line#?} ;;
        *) break ;;
      esac
    done
    case $line in
      "Include "*)
        # shellcheck disable=SC2016 # the generated script must hold a literal $MS_LIB
        printf '. "$MS_LIB/%s"\n' "${line#Include }"
        ;;
      "Describe "*)
        eval "text=${line#Describe }"
        depth=$((depth + 1))
        eval "kind_$depth=describe prev_$depth=\$prefix"
        # shellcheck disable=SC2154 # text is assigned by the eval above
        prefix=$prefix${prefix:+ }$text
        ;;
      "It "*)
        eval "text=${line#It }"
        func_no=$((func_no + 1))
        depth=$((depth + 1))
        eval "kind_$depth=it prev_$depth=\$prefix"
        prefix=$prefix${prefix:+ }$text
        printf 'ms_ex%s() {\n' "$func_no"
        ;;
      End)
        if [ "$depth" -eq 0 ]; then
          printf 'Syntax error: unexpected End at line %s\n' "$lineno" >&2
          exit 1
        fi
        eval "kind=\$kind_$depth"
        # shellcheck disable=SC2154 # kind is assigned by the eval above
        if [ "$kind" = it ]; then
          quoted=$(quote "$prefix")
          printf '}\nms_example %s ms_ex%s\n' "$quoted" "$func_no"
        fi
        eval "prefix=\$prev_$depth"
        depth=$((depth - 1))
        ;;
      "Data "*) printf 'ms_data %s\n' "${line#Data }" ;;
      "When "*) printf 'ms_when %s\n' "${line#When }" ;;
      "The "*) printf 'ms_the %s\n' "${line#The }" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done <"$spec"
  if [ "$depth" -ne 0 ]; then
    printf "Syntax error: unexpected end of file (expecting 'End')\n" >&2
    exit 1
  fi
  printf 'ms_end\n'
} >"$out"
