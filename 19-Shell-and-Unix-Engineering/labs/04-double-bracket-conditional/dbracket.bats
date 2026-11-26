#!/usr/bin/env bats
# Tests for the [[ conditional command across the shell matrix.
#
# Every snippet starts with `exec 2>/dev/null`, so diagnostics never reach the output and each
# assertion compares exact exit statuses. A shell that rejects the code at parse time prints its
# message before that redirection takes effect, so those cases assert a failed parse instead.
#
# shellcheck disable=SC2016 # every snippet's expansions belong to the shell under test

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/dbracket_probe.sh"
  FAMILY=$(lab_shell_family)
  cd "$BATS_TEST_TMPDIR" || return 1
}

# out_is LINE...: the last run printed exactly these lines.
out_is() {
  local want
  want=$(printf '%s\n' "$@")
  if [ "$output" != "$want" ]; then
    printf 'want:\n%s\ngot:\n%s\n' "$want" "$output" >&2
    return 1
  fi
}

# parse_failed: the shell rejected the code before running any of it (the leading echo never ran).
# The marker must be a whole line: bash quotes the offending source line, REACHED included, in
# its syntax error message.
parse_failed() {
  local line
  for line in "${lines[@]}"; do
    if [ "$line" = REACHED ]; then
      printf "expected a parse error, but the leading echo ran:\n%s\n" "$output" >&2
      return 1
    fi
  done
  if [ "$status" -eq 0 ]; then
    printf 'expected a parse error, got status %s and:\n%s\n' "$status" "$output" >&2
    return 1
  fi
}

# external_dbracket: print the path of a [[ executable on PATH; fail when there is none.
# BusyBox installs /usr/bin/[[ on Alpine, so there `"[["` and a keyword-less shell both find it.
external_dbracket() {
  type -P '[['
}

# no_dbracket LINE...: dash and posh have no [[ of their own.
# With no [[ executable on PATH every use is "command not found", so the output must be exactly
# LINE...; where an executable exists the shell must resolve [[ to that file, and the probe tests
# assert what the external utility then does.
no_dbracket() {
  local ext
  if ext=$(external_dbracket); then
    run_lab_c 'command -v "[["'
    [ "$output" = "$ext" ]
  else
    out_is "$@"
  fi
}

# bash_at_least MAJOR MINOR: true when the bash under test is at least that release.
bash_at_least() {
  local v major minor
  v=$(lab_shell_version)
  major=${v%%.*}
  minor=${v#*.}
  minor=${minor%%.*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# is_ksh93_2012: true when the shell under test is the AT&T ksh93u+ 2012 build (not ksh93u+m).
is_ksh93_2012() {
  [ "$FAMILY" = ksh93 ] || return 1
  case $(lab_shell_version) in
    *93u+m*) return 1 ;;
    *) return 0 ;;
  esac
}

# collating_locale: true when en_US.UTF-8 exists here and sorts a before B (not byte order).
collating_locale() {
  local sorted
  printf 'B\na\n' >"$BATS_TEST_TMPDIR/collate.in"
  sorted=$(LC_ALL=en_US.UTF-8 sort "$BATS_TEST_TMPDIR/collate.in" 2>/dev/null) || return 1
  [ "${sorted%%$'\n'*}" = a ]
}

@test "probe classify: [[ is a keyword, a builtin, an external utility, or absent" {
  run_lab "$PROBE" classify
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is "kind=keyword" ;;
    busybox) out_is "kind=builtin" ;;
    dash | posh)
      if external_dbracket; then
        out_is "kind=external"
      else
        out_is "kind=absent"
      fi
      ;;
    *) false ;;
  esac
}

@test "probe contrast: the same five checks through [ and through [[" {
  run_lab "$PROBE" contrast
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh-sh)
      out_is "split    single=2 double=0" "empty    single=0 double=1" \
        "pattern  single=1 double=0" "octal    single=1 double=0" "expr     single=2 double=0"
      ;;
    zsh)
      out_is "split    single=0 double=0" "empty    single=0 double=1" \
        "pattern  single=1 double=0" "octal    single=1 double=1" "expr     single=2 double=0"
      ;;
    ksh93 | mksh)
      out_is "split    single=2 double=0" "empty    single=0 double=1" \
        "pattern  single=1 double=0" "octal    single=1 double=1" "expr     single=0 double=0"
      ;;
    yash)
      out_is "split    single=2 double=0" "empty    single=0 double=1" \
        "pattern  single=1 double=0" "octal    single=1 double=1" "expr     single=2 double=2"
      ;;
    busybox)
      out_is "split    single=2 double=2" "empty    single=0 double=0" \
        "pattern  single=1 double=0" "octal    single=1 double=1" "expr     single=2 double=2"
      ;;
    dash)
      if external_dbracket; then
        # dash expands and splits the words, then BusyBox's [[ applet evaluates them.
        out_is "split    single=2 double=2" "empty    single=0 double=0" \
          "pattern  single=1 double=0" "octal    single=1 double=1" "expr     single=2 double=2"
      else
        out_is "split    single=2 double=127" "empty    single=0 double=127" \
          "pattern  single=1 double=127" "octal    single=1 double=127" "expr     single=2 double=127"
      fi
      ;;
    posh)
      # posh evaluates [ integers with its arithmetic evaluator: 010 is octal and 1+1 is 2.
      if external_dbracket; then
        no_dbracket unused
      else
        out_is "split    single=2 double=127" "empty    single=0 double=127" \
          "pattern  single=1 double=127" "octal    single=0 double=127" "expr     single=0 double=127"
      fi
      ;;
    *) false ;;
  esac
}

@test "the probe rejects an unknown mode with status 2" {
  run_lab "$PROBE" nosuchmode
  [ "$status" -eq 2 ]
  [[ $output == usage:* ]]
}

@test "an unquoted expansion is one word inside [[: no field splitting" {
  run_lab_c 'exec 2>/dev/null; v="a b"; [[ $v == "a b" ]]; echo $?; [ $v = "a b" ]; echo $?'
  case $FAMILY in
    bash | zsh-sh | ksh93 | mksh | yash) out_is 0 2 ;;
    zsh) out_is 0 0 ;; # native zsh does not split an unquoted expansion anywhere
    busybox) out_is 2 2 ;;
    dash | posh) no_dbracket 127 2 ;;
    *) false ;;
  esac
}

@test "no pathname expansion inside [[, although the same word matches two files" {
  : >ab
  : >ac
  run_lab_c 'exec 2>/dev/null; [[ a* == "a*" ]]; echo $?; set -- a*; echo $#'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is 0 2 ;;
    busybox) out_is 2 2 ;;
    dash | posh) no_dbracket 127 2 ;;
    *) false ;;
  esac
}

@test "an empty unquoted expansion stays an operand of [[ and vanishes from [" {
  run_lab_c 'exec 2>/dev/null; v=; [[ -n $v ]]; echo $?; [ -n $v ]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is 1 0 ;;
    busybox) out_is 0 0 ;;
    dash | posh) no_dbracket 127 0 ;;
    *) false ;;
  esac
}

@test "a file test takes a name with a space as one operand" {
  : >"a b"
  run_lab_c 'exec 2>/dev/null; f="a b"; [[ -f $f ]]; echo $?; [[ -e $f && ! -d $f ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is 0 0 ;;
    busybox) out_is 2 2 ;;
    dash | posh) no_dbracket 127 127 ;;
    *) false ;;
  esac
}

@test "\$* and \"\$@\" each become one word inside [[" {
  run_lab_c 'exec 2>/dev/null; set -- "a b" c; [[ $* == "a b c" ]]; echo $?; [[ "$@" == "a b c" ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is 0 0 ;;
    busybox) out_is 2 2 ;;
    dash | posh) no_dbracket 127 127 ;;
    *) false ;;
  esac
}

@test "the right side of == is a pattern, and quoting any part makes that part literal" {
  run_lab_c 'exec 2>/dev/null; [[ abc == a* ]]; echo $?; [[ abc == "a*" ]]; echo $?; [[ abc == a"*" ]]; echo $?; [[ "a*" == a"*" ]]; echo $?; [[ abc != a* ]]; echo $?; [[ a* == abc ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is 0 1 1 0 1 1 ;;
    busybox) out_is 0 0 0 0 1 1 ;; # fnmatch on the right operand whatever its quoting
    dash | posh) no_dbracket 127 127 127 127 127 127 ;;
    *) false ;;
  esac
}

@test "a pattern in an unquoted variable still matches: a guess of * equals any secret" {
  run_lab_c 'exec 2>/dev/null; secret=hunter2; guess="*"; [[ $secret == $guess ]]; echo $?; [[ $secret == "$guess" ]]; echo $?'
  case $FAMILY in
    bash | zsh-sh | ksh93 | mksh | yash) out_is 0 1 ;;
    zsh) out_is 1 1 ;; # native zsh treats an expansion as a pattern only as $~guess
    busybox) out_is 0 0 ;;
    dash | posh) no_dbracket 127 127 ;;
    *) false ;;
  esac
}

@test "a value with a backslash does not equal itself as an unquoted pattern" {
  run_lab_c 'exec 2>/dev/null; x="a\\b"; [[ $x == $x ]]; echo $?; [[ $x == "$x" ]]; echo $?'
  case $FAMILY in
    bash | ksh93 | mksh | yash) out_is 1 0 ;;
    zsh | zsh-sh) out_is 0 0 ;;
    busybox) out_is 1 1 ;;
    dash | posh) no_dbracket 127 127 ;;
    *) false ;;
  esac
}

@test "< and > compare strings without escaping and open no file" {
  run_lab_c 'exec 2>/dev/null; [[ a < b ]]; echo $?; [[ b < a ]]; echo $?; [[ a > b ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash)
      out_is 0 1 1
      [ ! -e b ]
      ;;
    busybox)
      # < and > stay redirections: the first two fail to open a file, the third creates b.
      out_is 1 1 0
      [ -e b ]
      ;;
    dash)
      if external_dbracket; then
        out_is 2 2 0
      else
        out_is 2 2 127
      fi
      [ -e b ]
      ;;
    posh)
      if external_dbracket; then
        out_is 1 1 0
      else
        out_is 1 1 127
      fi
      [ -e b ]
      ;;
    *) false ;;
  esac
}

@test "in the C locale < is byte order, so a sorts after B" {
  run_lab_c 'exec 2>/dev/null; LC_ALL=C; export LC_ALL; [[ a < B ]]; echo $?; [[ B < a ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is 1 0 ;;
    busybox) out_is 1 1 ;; # both are failed redirections
    dash) out_is 2 2 ;;
    posh) out_is 1 1 ;;
    *) false ;;
  esac
}

@test "in a collating locale bash 4.1+, ksh93 and yash order a before B; bash 3.2, zsh and mksh keep byte order" {
  local want_locale want_bytes
  run_lab_c 'exec 2>/dev/null; LC_ALL=en_US.UTF-8; export LC_ALL; [[ a < B ]]; echo $?'
  if collating_locale; then
    want_locale=0
  else
    want_locale=1 # no such locale here, or it sorts by code point: byte order everywhere
  fi
  want_bytes=1
  case $FAMILY in
    bash)
      if bash_at_least 4 1; then
        out_is "$want_locale"
      else
        out_is "$want_bytes"
      fi
      ;;
    ksh93 | yash) out_is "$want_locale" ;;
    zsh | zsh-sh | mksh) out_is "$want_bytes" ;;
    busybox | posh) out_is 1 ;; # a failed redirection from the missing file B
    dash) out_is 2 ;;
    *) false ;;
  esac
}

@test "&& binds tighter than || and ! negates one term" {
  run_lab_c 'exec 2>/dev/null; [[ -n a || -n b && -z c ]]; echo $?; [[ ! -n a ]]; echo $?; [[ ! -n a || -n b ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash | busybox) out_is 0 1 0 ;;
    dash | posh) no_dbracket 127 127 127 ;;
    *) false ;;
  esac
}

@test "parentheses group without quoting" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; [[ ( -n a || -n b ) && -z c ]]; echo $?; [[ ( -z a || -n b ) && -n c ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is REACHED 1 0 ;;
    busybox | dash | posh) parse_failed ;; # ( is the subshell operator to these parsers
    *) false ;;
  esac
}

@test "&& and || short-circuit: the skipped operand is never expanded, unlike [ -a" {
  run_lab_c 'exec 2>/dev/null
unset v; [[ -n "" && -n ${v:=set} ]]; echo "$? ${v-unset}"
unset w; [[ -n x || -n ${w:=set} ]]; echo "$? ${w-unset}"
unset u; [ -n "" -a -n "${u:=set}" ]; echo "$? ${u-unset}"'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is "1 unset" "0 unset" "1 set" ;;
    busybox) out_is "1 set" "0 set" "1 set" ;; # a builtin: every word is expanded first
    dash | posh) no_dbracket "127 unset" "127 set" "1 set" ;;
    *) false ;;
  esac
}

@test "[[ runs in the current shell: an assignment made by an operand persists" {
  run_lab_c 'exec 2>/dev/null; v=0; [[ -n ${v:=1} && -n $((v = v + 1)) ]]; echo "$v"'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash | busybox) out_is 1 ;;
    dash | posh) no_dbracket 0 ;;
    *) false ;;
  esac
}

@test "tilde, command substitution and arithmetic expansion happen; brace expansion does not" {
  run_lab_c 'exec 2>/dev/null; [[ ~ == "$HOME" ]]; echo $?; [[ "~" == "$HOME" ]]; echo $?; [[ {a,b} == "{a,b}" ]]; echo $?; [[ $(echo "a b") == "a b" ]]; echo $?; [[ $((1 + 1)) == 2 ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is 0 1 0 0 0 ;;
    busybox) out_is 0 1 0 2 0 ;;
    dash | posh) no_dbracket 127 127 127 127 127 ;;
    *) false ;;
  esac
}

@test "arithmetic operands are expressions: 1+1, a variable name, an unset name, an empty string" {
  run_lab_c 'exec 2>/dev/null; [[ 1+1 -eq 2 ]]; echo $?; y=5; x=y; [[ x -eq 5 ]]; echo $?; unset nosuch; [[ nosuch -eq 0 ]]; echo $?; v=; [[ $v -eq 0 ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh) out_is 0 0 0 0 ;;
    yash | busybox) out_is 2 2 2 2 ;; # integers only: anything else is an error
    dash | posh) no_dbracket 127 127 127 127 ;;
    *) false ;;
  esac
}

@test "an arithmetic operand can assign: y=7 as a value sets y" {
  run_lab_c 'exec 2>/dev/null; x="y=7"; [[ $x -eq 7 ]]; echo "$? ${y-unset}"'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh) out_is "0 7" ;;
    yash | busybox) out_is "2 unset" ;;
    dash | posh) no_dbracket "127 unset" ;;
    *) false ;;
  esac
}

@test "a leading zero is octal inside [[ in bash and zsh-sh, decimal elsewhere" {
  run_lab_c 'exec 2>/dev/null; [[ 010 -eq 8 ]]; echo $?; [[ 010 -eq 10 ]]; echo $?'
  case $FAMILY in
    bash | zsh-sh) out_is 0 1 ;;
    zsh | ksh93 | mksh | yash | busybox) out_is 1 0 ;;
    dash | posh) no_dbracket 127 127 ;;
    *) false ;;
  esac
}

@test "bash reports an arithmetic error as status 1, and ! turns it into 0" {
  run_lab_c 'exec 2>/dev/null; [[ 08 -eq 8 ]]; echo $?; [[ ! 08 -eq 8 ]]; echo $?; [[ 1.5 -lt 2 ]]; echo $?; [[ ! 1.5 -lt 2 ]]; echo $?'
  case $FAMILY in
    bash) out_is 1 0 1 0 ;;
    zsh | ksh93) out_is 0 1 0 1 ;; # 08 is decimal 8 and 1.5 is a float: no error at all
    zsh-sh) out_is "" ;;           # 08 is a bad octal constant: zsh abandons the whole command list
    mksh | yash | busybox) out_is 0 1 2 2 ;;
    dash | posh) no_dbracket 127 127 127 127 ;;
    *) false ;;
  esac
}

@test "integer range: 2^63-1 compares exactly and 2^63 wraps negative only in bash" {
  run_lab_c 'exec 2>/dev/null; [[ 9223372036854775807 -gt 9223372036854775806 ]]; echo $?; [[ 9223372036854775808 -lt 0 ]]; echo $?'
  case $FAMILY in
    bash) out_is 0 0 ;;
    zsh | zsh-sh | mksh) out_is 0 1 ;;
    ksh93)
      if is_ksh93_2012; then
        out_is 1 1 # floating point: the two operands of the first test compare equal
      else
        out_is 0 1
      fi
      ;;
    yash | busybox) out_is 0 2 ;;
    dash | posh) no_dbracket 127 127 ;;
    *) false ;;
  esac
}

@test "arithmetic injection: a value shaped like an array subscript runs a command, quoted or not" {
  run_lab_c 'exec 2>/dev/null; b=1; x="b[1\$(echo run >pwned1)]"; [[ $x -eq 0 ]]; echo $?
x="b[1\$(echo run >pwned2)]"; [[ "$x" -eq 0 ]]; echo $?'
  case $FAMILY in
    bash | mksh | zsh-sh)
      out_is 0 0
      [ -e pwned1 ] && [ -e pwned2 ]
      ;;
    zsh)
      out_is 1 1
      [ -e pwned1 ] && [ -e pwned2 ]
      ;;
    ksh93)
      if is_ksh93_2012; then
        out_is 0 0
        [ -e pwned1 ] && [ -e pwned2 ]
      else
        # ksh93u+m rejects the subscript as an arithmetic syntax error and abandons the command list.
        out_is ""
        [ ! -e pwned1 ] && [ ! -e pwned2 ]
      fi
      ;;
    yash)
      out_is 2 2
      [ ! -e pwned1 ] && [ ! -e pwned2 ]
      ;;
    busybox)
      out_is 2 2
      [ ! -e pwned1 ] && [ ! -e pwned2 ]
      ;;
    dash | posh)
      no_dbracket 127 127
      [ ! -e pwned1 ] && [ ! -e pwned2 ]
      ;;
    *) false ;;
  esac
}

@test "the same value through [ is rejected by bash, zsh, yash, dash and BusyBox ash" {
  run_lab_c 'exec 2>/dev/null; b=1; x="b[1\$(echo run >pwned)]"; [ "$x" -eq 0 ]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | yash | dash | busybox)
      out_is 2
      [ ! -e pwned ]
      ;;
    mksh)
      out_is 0
      [ -e pwned ]
      ;;
    ksh93)
      if is_ksh93_2012; then
        out_is 0
        [ -e pwned ]
      else
        out_is 2
        [ ! -e pwned ]
      fi
      ;;
    posh)
      # posh runs the substitution, then fails on the expression and abandons the command list.
      out_is ""
      [ -e pwned ]
      ;;
    *) false ;;
  esac
}

@test "-v tests whether a variable is set: bash 4.2+, zsh, ksh93 and mksh" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; v=; [[ -v v ]]; echo $?; unset w; [[ -v w ]]; echo $?'
  case $FAMILY in
    bash)
      if bash_at_least 4 2; then
        out_is REACHED 0 1
      else
        parse_failed
      fi
      ;;
    zsh | zsh-sh | ksh93 | mksh) out_is REACHED 0 1 ;;
    yash) parse_failed ;;
    busybox) out_is REACHED 2 2 ;;
    dash | posh) no_dbracket REACHED 127 127 ;;
    *) false ;;
  esac
}

@test "-v evaluates an array subscript: a name from input runs a command in bash 4.3+ and mksh" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; x="b[1\$(echo run >pwned)]"; [[ -v $x ]]; echo $?'
  case $FAMILY in
    bash)
      if bash_at_least 4 3; then
        out_is REACHED 1
        [ -e pwned ]
      else
        parse_failed
        [ ! -e pwned ]
      fi
      ;;
    mksh)
      out_is REACHED 1
      [ -e pwned ]
      ;;
    zsh | zsh-sh)
      out_is REACHED 1
      [ ! -e pwned ]
      ;;
    ksh93)
      # Both ksh93 releases stop at an arithmetic syntax error before running the substitution.
      out_is REACHED
      [ ! -e pwned ]
      ;;
    yash)
      parse_failed
      [ ! -e pwned ]
      ;;
    busybox)
      out_is REACHED 2
      [ ! -e pwned ]
      ;;
    dash | posh)
      no_dbracket REACHED 127
      [ ! -e pwned ]
      ;;
    *) false ;;
  esac
}

@test "-R is true for a name reference and -v follows it to the target: bash 4.3+ and ksh93" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; typeset -n r=t; t=1; [[ -R r ]]; echo $?; [[ -R t ]]; echo $?; [[ -v r ]]; echo $?; unset t; [[ -v r ]]; echo $?'
  case $FAMILY in
    bash)
      if bash_at_least 4 3; then
        out_is REACHED 0 1 0 1
      else
        parse_failed
      fi
      ;;
    ksh93) out_is REACHED 0 1 0 1 ;;
    zsh | zsh-sh) out_is REACHED ;; # -R is an unknown condition: zsh abandons the command list
    mksh | yash) parse_failed ;;
    busybox) out_is REACHED 2 2 2 2 ;;
    dash | posh) no_dbracket REACHED 127 127 127 127 ;;
    *) false ;;
  esac
}

@test "-o tests a shell option by name" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; set -o noglob; [[ -o noglob ]]; echo $?; set +o noglob; [[ -o noglob ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) out_is REACHED 0 1 ;;
    busybox) out_is REACHED 2 2 ;;
    dash | posh) no_dbracket REACHED 127 127 ;;
    *) false ;;
  esac
}

@test "=~ matches an extended regular expression everywhere but mksh" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; [[ abc =~ ^a.c$ ]]; echo $?; [[ abd =~ ^a.c$ ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | yash | busybox) out_is REACHED 0 1 ;;
    mksh) parse_failed ;;
    dash | posh) no_dbracket REACHED 127 127 ;;
    *) false ;;
  esac
}

@test "an invalid regular expression is status 2 in bash and BusyBox, 1 elsewhere, and ! hides it" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; re="("; [[ a =~ $re ]]; echo $?; [[ ! a =~ $re ]]; echo $?'
  case $FAMILY in
    bash) out_is REACHED 2 0 ;;
    busybox) out_is REACHED 2 2 ;;
    zsh | zsh-sh | ksh93 | yash) out_is REACHED 1 0 ;;
    mksh) parse_failed ;;
    dash | posh) no_dbracket REACHED 127 127 ;;
    *) false ;;
  esac
}

@test "an operator must be written in the source: one that arrives by expansion is a syntax error" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; op="=="; [[ a $op a ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) parse_failed ;;
    busybox) out_is REACHED 0 ;; # a builtin sees == as an ordinary argument
    dash | posh) no_dbracket REACHED 127 ;;
    *) false ;;
  esac
}

@test "a quoted operator is a plain word, except in ksh93" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; [[ a "==" a ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | mksh | yash) parse_failed ;;
    ksh93 | busybox) out_is REACHED 0 ;;
    dash | posh) no_dbracket REACHED 127 ;;
    *) false ;;
  esac
}

@test "a value that looks like an operator is still data" {
  run_lab_c 'exec 2>/dev/null; x=-f; [[ $x ]]; echo $?; x="!"; [[ $x ]]; echo $?; x="("; [[ $x == "(" ]]; echo $?; [[ "-f" ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash | busybox) out_is 0 0 0 0 ;;
    dash | posh) no_dbracket 127 127 127 127 ;;
    *) false ;;
  esac
}

@test "-a and -o are not binary operators inside [[" {
  local op
  for op in -a -o; do
    run_lab_c "echo REACHED; exec 2>/dev/null; [[ -n a $op -n b ]]; echo \$?"
    case $FAMILY in
      bash | ksh93 | mksh | yash) parse_failed ;;
      zsh | zsh-sh)
        # zsh parses "-n a -a -n b" as one call of an unknown multi-argument condition, and that
        # error at run time abandons the command list.
        [ "$status" -eq 2 ]
        out_is REACHED
        ;;
      busybox) out_is REACHED 2 ;;
      dash | posh) no_dbracket REACHED 127 ;;
      *) false ;;
    esac
  done
}

@test "[[ a==b ]] has no operator: it is one non-empty word and therefore true" {
  run_lab_c 'exec 2>/dev/null; [[ a==b ]]; echo $?; [[ a==a ]]; echo $?; [[ a == b ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash | busybox) out_is 0 0 1 ;;
    dash | posh) no_dbracket 127 127 127 ;;
    *) false ;;
  esac
}

@test "an empty or operator-only expression is a syntax error, not false" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; [[ ]]; echo $?'
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) parse_failed ;;
    busybox) out_is REACHED 1 ;;
    dash | posh) no_dbracket REACHED 127 ;;
    *) false ;;
  esac
  run_lab_c 'echo REACHED; exec 2>/dev/null; [[ -f ]]; echo $?'
  case $FAMILY in
    bash | ksh93 | mksh | yash) parse_failed ;;
    zsh | zsh-sh) out_is REACHED ;; # an "unknown condition" error at run time ends the command list
    busybox) out_is REACHED 0 ;;    # one argument: a non-empty string
    dash | posh) no_dbracket REACHED 127 ;;
    *) false ;;
  esac
}

@test "a syntax error inside [[ stops the script at that line; the same mistake in [ only returns 2" {
  run_lab_c 'exec 2>/dev/null
echo before
[[ a b ]]
echo after'
  case $FAMILY in
    bash | ksh93 | mksh | yash)
      [ "$status" -ne 0 ]
      out_is before
      ;;
    zsh | zsh-sh)
      # zsh parses the whole -c string before it runs anything.
      [ "$status" -ne 0 ]
      [[ $output != *before* ]]
      ;;
    busybox)
      [ "$status" -eq 0 ]
      out_is before after
      ;;
    dash | posh)
      [ "$status" -eq 0 ]
      out_is before after
      ;;
    *) false ;;
  esac
  run_lab_c 'exec 2>/dev/null
echo before
[ a b ]
echo "after $?"'
  [ "$status" -eq 0 ]
  out_is before "after 2"
}

@test "set -e: a false [[ exits the shell, except in bash before 4.1" {
  run_lab_c 'exec 2>/dev/null; set -e; [[ a == b ]]; echo survived'
  case $FAMILY in
    bash)
      if bash_at_least 4 1; then
        [ "$status" -eq 1 ]
        out_is ""
      else
        [ "$status" -eq 0 ]
        out_is survived
      fi
      ;;
    zsh | zsh-sh | ksh93 | mksh | yash | busybox)
      [ "$status" -eq 1 ]
      out_is ""
      ;;
    dash | posh)
      if external_dbracket; then
        [ "$status" -eq 1 ]
      else
        [ "$status" -eq 127 ]
      fi
      out_is ""
      ;;
    *) false ;;
  esac
}

@test "extended patterns work on the right of == with no shopt in bash 4.1+, ksh93 and mksh" {
  run_lab_c 'echo REACHED; exec 2>/dev/null; [[ ab == @(ab|cd) ]]; echo $?; [[ ef == @(ab|cd) ]]; echo $?'
  case $FAMILY in
    bash)
      if bash_at_least 4 1; then
        out_is REACHED 0 1
      else
        parse_failed
      fi
      ;;
    ksh93 | mksh) out_is REACHED 0 1 ;;
    zsh) out_is REACHED 1 1 ;; # without KSH_GLOB the parentheses are a plain group after a literal @
    zsh-sh | yash | busybox | dash | posh) parse_failed ;;
    *) false ;;
  esac
}

@test "a quoted [[ is not the keyword: the shell looks for a command with that name" {
  run_lab_c 'exec 2>/dev/null; v="a b"; "[[" $v = "a b" ]]; echo $?'
  if external_dbracket; then
    # Every shell runs the external utility, which gets the split words (one word from native zsh).
    case $FAMILY in
      zsh) out_is 0 ;;
      bash | zsh-sh | ksh93 | mksh | yash | busybox | dash | posh) out_is 2 ;;
      *) false ;;
    esac
  else
    case $FAMILY in
      bash | zsh | zsh-sh | ksh93 | mksh | yash | dash | posh) out_is 127 ;;
      busybox) out_is 2 ;; # still the builtin, and still split
      *) false ;;
    esac
  fi
}
