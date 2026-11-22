#!/bin/sh
# Probe for the runtime facts behind the main ShellCheck warning families.
# Each mode runs the hazardous form and the fixed form side by side and prints computed key=value lines.
# The hazardous forms are deliberate, so each carries a disable comment naming the code it triggers.
# usage: families-probe.sh MODE
#   quoting   SC2086 SC2046: word splitting and pathname expansion of unquoted expansions
#   masking   SC2155 SC2312 SC2005: command substitution statuses hidden by the surrounding command
#   status    SC2181: testing $? after an unrelated command, and under set -e
#   cd        SC2164: continuing in the wrong directory after a failed cd
#   andor     SC2015: A && B || C is not if-then-else
#   read      SC2162: backslash and whitespace handling in read
#   trap      SC2064: when a double-quoted trap string expands
#   unset     SC2154: a misspelled variable expands to nothing, and set -u catches it
#   arrays    SC2128 SC3030: array syntax across shell families
#   dashglob  SC2035: a file named like an option (-u is the one cat flag POSIX defines) when a glob is the first operand
#   lines     SC2013: for over $(cat) versus while read
#   errexit   SC2310 SC2311: set -e inside a command substitution and in an if condition

set -u

usage() {
  printf 'usage: %s quoting|masking|status|cd|andor|read|trap|unset|arrays|dashglob|lines|errexit\n' "$0" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
mode=$1

# count ARG...: print the number of arguments it received.
count() {
  printf '%s' "$#"
}

mode_quoting() {
  dir=$(mktemp -d "${TMPDIR:-/tmp}/families.XXXXXX") || exit 1
  trap 'rm -rf "$dir"' EXIT
  cd "$dir" || exit 1
  : >a.txt
  : >b.txt
  : >'c d.txt'
  unset IFS
  v='x y'
  g='*.txt'
  e=''
  # shellcheck disable=SC2086,SC2248 # deliberate hazard: unquoted expansion splits
  n=$(count $v)
  printf 'split=%s\n' "$n"
  n=$(count "$v")
  printf 'quoted-split=%s\n' "$n"
  # shellcheck disable=SC2086,SC2248 # deliberate hazard: unquoted expansion globs
  n=$(count $g)
  printf 'glob=%s\n' "$n"
  n=$(count "$g")
  printf 'quoted-glob=%s\n' "$n"
  # shellcheck disable=SC2086,SC2248 # deliberate hazard: an empty unquoted expansion vanishes
  n=$(count $e)
  printf 'unquoted-empty=%s\n' "$n"
  n=$(count "$e")
  printf 'quoted-empty=%s\n' "$n"
  # shellcheck disable=SC2086,SC2248 # the alternate-value idiom: ShellCheck accepts the inner quotes, not the outer expansion
  n=$(count ${e:+"$e"})
  printf 'alt-value-empty=%s\n' "$n"
  # shellcheck disable=SC2086,SC2248 # exception under test: set -f stops globbing
  n=$(
    set -f
    count $g
  )
  printf 'noglob=%s\n' "$n"
  # shellcheck disable=SC2086,SC2248 # exception under test: an empty IFS stops splitting
  n=$(
    IFS=''
    count $v
  )
  printf 'ifs-empty=%s\n' "$n"
  # shellcheck disable=SC2046,SC2312 # deliberate hazard: unquoted command substitution splits
  n=$(count $(printf 'p q\n'))
  printf 'cmdsubst-split=%s\n' "$n"
  inner=$(printf 'p q\n')
  n=$(count "$inner")
  printf 'cmdsubst-quoted=%s\n' "$n"
}

mode_masking() {
  # export is the command, so the substitution's status is discarded (POSIX.1-2024 XCU 2.15, export)
  # shellcheck disable=SC2155,SC2046,SC2034 # deliberate hazard: export discards the substitution status
  export masked=$(false)
  printf 'export-assign=%s\n' "$?"
  # shellcheck disable=SC2034 # the value is deliberately unused; only the status matters
  unmasked=$(false)
  printf 'plain-assign=%s\n' "$?"
  # shellcheck disable=SC2312,SC2034 # deliberate: only the last substitution's status survives; the value is unused
  x="$(true)/$(false)"
  printf 'last-false=%s\n' "$?"
  # shellcheck disable=SC2312,SC2034 # deliberate: only the last substitution's status survives; the value is unused
  x="$(false)/$(true)"
  printf 'first-false=%s\n' "$?"
  r=$(
    set -e
    # shellcheck disable=SC2155,SC2046,SC2034 # deliberate hazard: export discards the substitution status
    export y=$(false)
    printf 'reached'
  )
  printf 'errexit-export=%s\n' "${r:-stopped}"
  r=$(
    set -e
    y=$(false)
    printf 'reached'
  )
  printf 'errexit-plain=%s\n' "${r:-stopped}"
  # shellcheck disable=SC2005,SC2312 # deliberate: echo hides the failing command's status
  echo "$(false)" >/dev/null
  # shellcheck disable=SC2320 # deliberate: $? is the status of echo
  printf 'useless-echo=%s\n' "$?"
}

mode_status() {
  false
  printf 'noise\n' >/dev/null
  # shellcheck disable=SC2181 # deliberate hazard: $? is now printf's status
  if [ $? -ne 0 ]; then
    printf 'indirect=detected\n'
  else
    printf 'indirect=missed\n'
  fi
  if ! false; then
    printf 'direct=detected\n'
  else
    printf 'direct=missed\n'
  fi
  r=$(
    set -e
    false
    # shellcheck disable=SC2181 # deliberate hazard: errexit exits before the test runs
    if [ $? -ne 0 ]; then
      printf 'handler-ran'
    fi
  )
  printf 'errexit-indirect=%s\n' "${r:-handler-skipped}"
  r=$(
    set -e
    if ! false; then
      printf 'handler-ran'
    fi
  )
  printf 'errexit-direct=%s\n' "${r:-handler-skipped}"
}

mode_cd() {
  dir=$(mktemp -d "${TMPDIR:-/tmp}/families.XXXXXX") || exit 1
  trap 'rm -rf "$dir"' EXIT
  (
    cd "$dir" || exit 1
    # shellcheck disable=SC2164 # deliberate hazard: the failed cd is ignored
    cd missing 2>/dev/null
    printf 'cd-status=%s\n' "$?"
    : >marker-unchecked
  )
  (
    cd "$dir" || exit 1
    cd missing 2>/dev/null || exit 0
    : >marker-checked
  )
  if [ -e "$dir/marker-unchecked" ]; then
    printf 'unchecked=wrote-in-wrong-directory\n'
  else
    printf 'unchecked=safe\n'
  fi
  if [ -e "$dir/marker-checked" ]; then
    printf 'checked=wrote-in-wrong-directory\n'
  else
    printf 'checked=safe\n'
  fi
}

mode_andor() {
  # shellcheck disable=SC2015 # deliberate hazard: B fails, so C runs although A was true
  true && false || printf 'and-or-ran-c\n'
  if true; then
    false
  else
    printf 'if-ran-c\n'
  fi
  printf 'if-done\n'
}

mode_read() {
  printf '%s\n' 'a\b' | {
    read -r raw
    printf 'raw=%s\n' "$raw"
  }
  # shellcheck disable=SC2162 # deliberate hazard: read without -r treats backslash as an escape
  printf '%s\n' 'a\b' | {
    read cooked
    printf 'cooked=%s\n' "$cooked"
  }
  # shellcheck disable=SC2162 # deliberate hazard: backslash-newline joins two input lines
  printf '%s\n' "x\\" y | {
    read joined
    printf 'joined=%s\n' "$joined"
  }
  printf '%s\n' "x\\" y | {
    read -r first
    printf 'joined-raw=%s\n' "$first"
  }
  printf '%s\n' '  pad  ' | {
    read -r trimmed
    printf 'trimmed=<%s>\n' "$trimmed"
  }
  printf '%s\n' '  pad  ' | {
    IFS= read -r kept
    printf 'kept=<%s>\n' "$kept"
  }
}

mode_trap() {
  early=$(
    v=early
    # shellcheck disable=SC2064 # deliberate hazard: the double-quoted trap expands $v now
    trap "printf '%s\\n' $v" EXIT
    v=late
  )
  printf 'double-quoted=%s\n' "$early"
  late=$(
    v=early
    trap 'printf "%s\\n" "$v"' EXIT
    v=late
  )
  printf 'single-quoted=%s\n' "$late"
}

mode_unset() {
  # the file-wide set -u would abort the plain read below, which is the hazard being shown
  set +u
  target=world
  # shellcheck disable=SC2154 # deliberate hazard: tagret is a misspelling of target
  printf 'plain=<hello %s>\n' "$tagret"
  r=$(
    {
      set -u
      # shellcheck disable=SC2154 # deliberate hazard: tagret is a misspelling of target
      printf 'hello %s' "$tagret"
    } 2>/dev/null
  )
  printf 'set-u-status=%s\n' "$?"
  printf 'set-u-output=<%s>\n' "$r"
  printf 'target=%s\n' "$target"
}

mode_arrays() {
  # the construct sits in an eval string so a shell that rejects it fails only this command
  out=$(
    eval 'a=(foo bar); printf "<%s>" $a' 2>/dev/null
  )
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf 'array=rejected\n'
  else
    printf 'array=%s\n' "$out"
  fi
}

# contents_seen TEXT: report which of the two fixture files' contents (x and y) appear in TEXT.
contents_seen() {
  case $1 in
    *x*y* | *y*x*) printf 'x-and-y' ;;
    *x*) printf 'x-only' ;;
    *y*) printf 'y-only' ;;
    *) printf 'neither' ;;
  esac
}

mode_dashglob() {
  dir=$(mktemp -d "${TMPDIR:-/tmp}/families.XXXXXX") || exit 1
  trap 'rm -rf "$dir"' EXIT
  cd "$dir" || exit 1
  LC_ALL=C
  export LC_ALL
  printf 'x\n' >./-u
  printf 'y\n' >b.txt
  # shellcheck disable=SC2035 # deliberate hazard: the file named -u becomes an option
  unsafe=$(/bin/cat * 2>&1)
  seen=$(contents_seen "$unsafe")
  printf 'unsafe=%s\n' "$seen"
  prefixed=$(/bin/cat ./* 2>&1)
  seen=$(contents_seen "$prefixed")
  printf 'prefixed=%s\n' "$seen"
  ended=$(/bin/cat -- * 2>&1)
  seen=$(contents_seen "$ended")
  printf 'ended=%s\n' "$seen"
}

mode_errexit() {
  # f fails at its first command; a shell whose substitution inherits errexit stops there
  r=$(
    set -e
    f() {
      false
      printf 'f-continued'
    }
    x=$(f)
    printf 'after:%s' "$x"
  )
  case $r in
    "") printf 'subst-inherits-errexit=yes\n' ;;
    *) printf 'subst-inherits-errexit=no\n' ;;
  esac
  r=$(
    set -e
    f() {
      false
      printf 'f-continued'
    }
    # shellcheck disable=SC2310 # deliberate hazard: errexit is off while f runs as an if condition
    if f; then
      printf 'condition-true'
    fi
  )
  printf 'condition-output=%s\n' "$r"
}

mode_lines() {
  dir=$(mktemp -d "${TMPDIR:-/tmp}/families.XXXXXX") || exit 1
  trap 'rm -rf "$dir"' EXIT
  cd "$dir" || exit 1
  : >aa.txt
  : >bb.txt
  printf 'foo *\nbar\n' >list
  unset IFS
  words=0
  # shellcheck disable=SC2013,SC2248,SC2312 # deliberate hazard: for reads words and expands globs
  for w in $(cat list); do
    words=$((words + 1))
    case $w in
      aa.txt | bb.txt) printf 'expanded=%s\n' "$w" ;;
      *) ;;
    esac
  done
  printf 'for-words=%s\n' "$words"
  rows=0
  # shellcheck disable=SC2034 # the loop only counts lines; the value is unused
  while IFS= read -r line; do
    rows=$((rows + 1))
  done <list
  printf 'while-lines=%s\n' "$rows"
}

case $mode in
  quoting) mode_quoting ;;
  masking) mode_masking ;;
  status) mode_status ;;
  cd) mode_cd ;;
  andor) mode_andor ;;
  read) mode_read ;;
  trap) mode_trap ;;
  unset) mode_unset ;;
  arrays) mode_arrays ;;
  dashglob) mode_dashglob ;;
  lines) mode_lines ;;
  errexit) mode_errexit ;;
  *) usage ;;
esac
