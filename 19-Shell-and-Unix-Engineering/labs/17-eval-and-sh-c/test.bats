#!/usr/bin/env bats
# Lab 17: eval and sh -c, the two places where the shell turns a string back into code.

setup() {
  load ../lib/lab
  QE="$BATS_TEST_DIRNAME/quote-and-eval.sh"
  FX="$BATS_TEST_DIRNAME/find-exec.sh"
}

# dash_at_least_0_5_13: succeed if the dash under test is 0.5.13 or later, judged from the package
# metadata of the host that provides it, not from the behavior under test, so the assertion that
# uses it cannot agree with itself by construction. dash has no version variable.
# dash 0.5.13 added $'...' (dash git commit "parser: Add dollar single quote", 2024-06-22).
dash_at_least_0_5_13() {
  local version=""
  if command -v dpkg-query >/dev/null 2>&1; then
    # shellcheck disable=SC2016 # ${Version} is dpkg-query's format field, not a shell expansion
    version=$(dpkg-query -W -f='${Version}' dash 2>/dev/null)
  elif command -v apk >/dev/null 2>&1; then
    version=$(apk info -v dash 2>/dev/null)
    version=${version#dash-}
  elif command -v brew >/dev/null 2>&1; then
    version=$(brew list --versions dash-shell 2>/dev/null)
    version=${version#dash-shell }
  fi
  case $version in
    0.5.1[3-9]* | 0.5.[2-9]* | 0.[6-9]* | [1-9]*) return 0 ;;
    0.5.* | 0.[0-4].*) return 1 ;;
    *)
      printf 'cannot determine the dash version (got "%s")\n' "$version" >&2
      return 2
      ;;
  esac
}

# bash_at_least MAJOR MINOR: succeed if the bash under test is at least MAJOR.MINOR.
bash_at_least() {
  local version major rest minor
  version=$(lab_shell_version)
  major=${version%%.*}
  rest=${version#*.}
  minor=${rest%%[!0-9]*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

@test "sh -c binds command_name to \$0 and the remaining operands to \$1 onward" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'printf "<%s>" "$0" "$#" "$@"' name a 'b c' ''
  [ "$status" -eq 0 ]
  [ "$output" = '<name><3><a><b c><>' ]
}

@test "omitting command_name silently moves the first data operand into \$0" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'printf "<%s>" "$0" "$#" "$@"' 'first file' second
  [ "$status" -eq 0 ]
  [ "$output" = '<first file><1><second>' ]
}

@test "the shell accepts -- between -c and the command string, as POSIX.1-2024 system() passes it" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab -c -- 'printf "<%s>" "$0" "$1"' name arg
  [ "$status" -eq 0 ]
  [ "$output" = '<name><arg>' ]
}

@test "data spliced into a -c string is parsed as code; the same data as an operand is not" {
  payload='x; echo INJECTED'
  run_lab_c "printf '<%s>\n' $payload"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '<x>' ]
  [ "${lines[1]}" = INJECTED ]

  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c 'printf "<%s>\n" "$1"' sh "$payload"
  [ "$status" -eq 0 ]
  [ "$output" = '<x; echo INJECTED>' ]
}

@test "quotes around spliced data do not help: a quote or a command substitution in the data escapes" {
  sq_payload="a'; echo INJECTED; echo '"
  run_lab_c "printf '<%s>\n' '$sq_payload'"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '<a>' ]
  [ "${lines[1]}" = INJECTED ]

  # shellcheck disable=SC2016 # the literal $(...) is the untrusted payload
  dq_payload='$(echo INJECTED)'
  run_lab_c "printf '<%s>\n' \"$dq_payload\""
  [ "$status" -eq 0 ]
  [ "$output" = '<INJECTED>' ]
}

@test "eval joins its operands with spaces and re-parses them, the way ssh joins a remote command" {
  run_lab "$QE" eval-join show 'a b' 'c;echo INJECTED'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '3 args: <a> <b> <c>' ]
  [ "${lines[1]}" = INJECTED ]

  run_lab "$QE" eval-quoted show 'a b' 'c;echo INJECTED'
  [ "$status" -eq 0 ]
  [ "$output" = '2 args: <a b> <c;echo INJECTED>' ]
}

@test "shquote wraps each word in single quotes and turns every quote into '\\''" {
  run_lab "$QE" quote "it's" '' 'a b'
  [ "$status" -eq 0 ]
  [ "$output" = "'it'\\''s' '' 'a b'" ]
}

@test "quoted words survive a second parse by another shell's -c byte for byte (the ssh case)" {
  # shellcheck disable=SC2016 # every $ and backquote here is payload, never expanded by bats
  set -- "it's" '' '$(echo INJECTED)' '`echo INJECTED`' 'a\b' '*' '-n' '"dq"' \
    "$(printf 'two\nlines')" "$(printf 'c\001d')" $'trail\n\n' "'" "''" "\\"
  run_lab "$QE" quote "$@"
  [ "$status" -eq 0 ]
  quoted=$output

  run_lab_c "printf '<%s>' $quoted"
  [ "$status" -eq 0 ]
  expected=$(printf '<%s>' "$@")
  [ "$output" = "$expected" ]
}

@test "eval of quoted words keeps every hostile word intact inside one shell" {
  # shellcheck disable=SC2016 # every $ and backquote here is payload, never expanded by bats
  set -- "it's" '' '$(echo INJECTED)' '`echo INJECTED`' 'a\b' '*' "$(printf 'x\ny')" $'trail\n\n'
  run_lab "$QE" eval-quoted show "$@"
  [ "$status" -eq 0 ]
  expected="$#"' args:'$(printf ' <%s>' "$@")
  [ "$output" = "$expected" ]
}

@test "eval NAME=\\\$2 never re-parses the value; eval NAME=\$value does; a bad NAME is refused" {
  # shellcheck disable=SC2016 # the literal $(...) is the untrusted payload
  run_lab "$QE" setvar v 'x;echo INJECTED $(echo SUB)'
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # the expected value keeps the literal $(...)
  [ "$output" = 'v=x;echo INJECTED $(echo SUB)' ]

  run_lab "$QE" setvar-unsafe v 'x;echo INJECTED'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = INJECTED ]
  [ "${lines[1]}" = 'v=x' ]

  run_lab "$QE" setvar 'v;echo INJECTED' x
  [ "$status" -eq 2 ]
  [ "$output" = 'invalid name: v;echo INJECTED' ]
}

@test "bash-style \$'...' quoting (printf %q output) is not portable to every sh" {
  run_lab_c "printf '<%s>' \$'a\\nb'"
  [ "$status" -eq 0 ]
  got=$output
  parsed=$(printf '<a\nb>')
  # shellcheck disable=SC2016 # the literal $ is what a shell without $'...' prints
  literal='<$a\nb>'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | ksh93 | mksh | yash | busybox) [ "$got" = "$parsed" ] ;;
    posh) [ "$got" = "$literal" ] ;;
    dash)
      # dash 0.5.13 and later parse $'...'; 0.5.12 (Debian, Alpine) reads a literal $.
      # A status other than 0 or 1 means the version is unknown, which fails the test.
      dash_new=0
      dash_at_least_0_5_13 || dash_new=$?
      case $dash_new in
        0) [ "$got" = "$parsed" ] ;;
        1) [ "$got" = "$literal" ] ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "some shells exec the last command of a -c string in place of themselves" {
  if [ -r /proc/self/comm ]; then
    # shellcheck disable=SC2016 # $$ belongs to the shell under test
    code=': ; /bin/cat /proc/$$/comm'
  else
    # shellcheck disable=SC2016 # $$ belongs to the shell under test
    code=': ; /bin/ps -o comm= -p $$'
  fi
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $output in
    cat | ps | */ps) replaced=yes ;;
    *) replaced=no ;;
  esac
  case $(lab_shell_family) in
    zsh | zsh-sh | ksh93 | yash | busybox) [ "$replaced" = yes ] ;;
    mksh | posh) [ "$replaced" = no ] ;;
    bash)
      if bash_at_least 5 1; then
        [ "$replaced" = yes ]
      else
        [ "$replaced" = no ]
      fi
      ;;
    dash)
      # Upstream dash execs the last command (since 2011-07-07); Debian's patch
      # 0004-SHELL-Disable-sh-c-command-sh-c-exec-command-optimiza.diff calls
      # evalstring(minusc, 0) and so forks, in 0.5.12-12 and 0.5.13.5-1 alike.
      # Measured: Debian dash forks; Alpine dash 0.5.12 and Homebrew dash 0.5.13.5 exec.
      if [ -r /etc/debian_version ]; then
        [ "$replaced" = no ]
      else
        [ "$replaced" = yes ]
      fi
      ;;
    *) false ;;
  esac
}

@test "command_name names the shell in diagnostics, except in zsh and yash" {
  run_lab_c 'nonexistent_cmd_q7' myname
  [ "$status" -eq 127 ]
  case $(lab_shell_family) in
    zsh | zsh-sh) [[ "$output" == zsh:* ]] ;;
    # Measured: yash 2.58.1 prints "yash: no such command `nonexistent_cmd_q7'".
    yash) [[ "$output" == yash:* ]] ;;
    # Measured: Debian's mksh R59 2025/04/26 prefixes "E: "; Alpine's R59 2020/10/31 does not.
    mksh) [[ "$output" == myname:* || "$output" == 'E: myname:'* ]] ;;
    bash | dash | ksh93 | posh | busybox) [[ "$output" == myname:* ]] ;;
    *) false ;;
  esac
}

@test "eval changes the current shell; sh -c runs in a child that cannot" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'x=1; eval "x=2"; sh -c "x=3"; printf "%s" "$x"'
  [ "$status" -eq 0 ]
  [ "$output" = 2 ]
}

@test "find -exec with {} spliced into sh -c runs a file name as code; {} as an operand does not" {
  dir=$BATS_TEST_TMPDIR/tree
  mkdir "$dir"
  : >"$dir/x\$(echo INJECTED)"

  run_lab "$FX" "$dir" embed
  [ "$status" -eq 0 ]
  [ "$output" = "<$dir/xINJECTED>" ]

  run_lab "$FX" "$dir" argv
  [ "$status" -eq 0 ]
  [ "$output" = "<$dir/x\$(echo INJECTED)>" ]
}

@test "find -exec sh -c ... sh {} + hands one shell every name as a separate operand" {
  dir=$BATS_TEST_TMPDIR/tree
  mkdir "$dir"
  : >"$dir/x\$(echo INJECTED)"
  : >"$dir/it's"

  run_lab "$FX" "$dir" batch
  [ "$status" -eq 0 ]
  [[ "$output" == '2 names:'* ]]
  [[ "$output" == *" <$dir/x\$(echo INJECTED)>"* ]]
  [[ "$output" == *" <$dir/it's>"* ]]
}
