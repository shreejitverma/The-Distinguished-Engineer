#!/usr/bin/env bats
# Tests for IFS attacks: environment import, literal versus expanded text, whitespace-free
# payloads, delimiter rules, IFS leaks, and the "$*" join.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/ifs_probe.sh"
}

# spawn VALUE: start the shell under test with IFS=VALUE in its environment, and run `state` in it.
spawn() {
  lab_shell_words
  run_lab "$PROBE" import "$1" "${LAB_SHELL_WORDS[@]}"
}

@test "an IFS in the environment is replaced by the default, except in posh" {
  spawn ':'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh-sh | dash | busybox | mksh | yash | ksh93)
      [ "${lines[0]}" = "ifs_hex: 20090a" ]
      ;;
    zsh) [ "${lines[0]}" = "ifs_hex: 20090a00" ] ;;
    posh) [ "${lines[0]}" = "ifs_hex: 3a" ] ;;
    *) false ;;
  esac
}

@test "an inherited IFS=/ does not split /bin/echo, except in posh" {
  spawn '/'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | busybox | mksh | yash | ksh93)
      [ "${lines[1]}" = "path_fields: 1" ]
      ;;
    posh) [ "${lines[1]}" = "path_fields: 3" ] ;;
    *) false ;;
  esac
}

@test "IFS=/ splits the result of an expansion but never literal text" {
  run_lab "$PROBE" literal
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "expansion: 3:<><bin><echo>" ]
  [ "${lines[1]}" = "literal: 1:</bin/echo>" ]
}

@test "native zsh does not split an unquoted expansion, so a whitespace substitute fails there" {
  run_lab "$PROBE" native_split
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh-sh | dash | busybox | mksh | yash | ksh93 | posh)
      [ "${lines[0]}" = "unquoted_fields: 2" ]
      [ "${lines[1]}" = "ifs_word: <hi> status=0" ]
      ;;
    zsh)
      [ "${lines[0]}" = "unquoted_fields: 1" ]
      [ "${lines[1]}" = "ifs_word: <> status=127" ]
      ;;
    *) false ;;
  esac
}

@test "\${IFS} and \$IFS\$9 stand in for a space; a null or unset IFS removes the trick" {
  run_lab "$PROBE" bypass
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "braced: <a b> status=0" ]
  [ "${lines[1]}" = "dollar9: <a> status=0" ]
  [ "${lines[2]}" = "null_ifs: <> status=127" ]
  [ "${lines[3]}" = "unset_ifs: <> status=127" ]
}

@test "a no-whitespace filter rejects a spaced payload and passes the IFS form to eval" {
  run_lab "$PROBE" filter 'hello; echo injected'
  [ "$status" -eq 2 ]
  [ "$output" = "rejected" ]
  # shellcheck disable=SC2016 # the dollar sign is part of the payload, expanded later by eval
  run_lab "$PROBE" filter 'hello;echo${IFS}injected'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "hello" ]
  [ "${lines[1]}" = "injected" ]
  [ "${#lines[@]}" -eq 2 ]
}

@test "the same payload is inert when the sink does not evaluate it" {
  # shellcheck disable=SC2016 # the dollar sign is part of the payload
  run_lab "$PROBE" filter_safe 'hello;echo${IFS}injected'
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # compared literally
  [ "$output" = 'hello;echo${IFS}injected' ]
}

@test "non-whitespace IFS characters terminate fields: a trailing delimiter adds no field except in zsh" {
  run_lab "$PROBE" fields : 'a:b:c'
  [ "$status" -eq 0 ]
  [ "$output" = "3:<a><b><c>" ]
  run_lab "$PROBE" fields : 'a:b:c:'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | busybox | mksh | yash | ksh93 | posh)
      [ "$output" = "3:<a><b><c>" ]
      ;;
    zsh | zsh-sh) [ "$output" = "4:<a><b><c><>" ] ;;
    *) false ;;
  esac
  run_lab "$PROBE" fields : 'a:b:c::'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | busybox | mksh | yash | ksh93 | posh)
      [ "$output" = "4:<a><b><c><>" ]
      ;;
    zsh | zsh-sh) [ "$output" = "5:<a><b><c><><>" ] ;;
    *) false ;;
  esac
}

@test "a leading non-whitespace delimiter makes an empty first field in every shell" {
  run_lab "$PROBE" fields : ':a'
  [ "$status" -eq 0 ]
  [ "$output" = "2:<><a>" ]
}

@test "whitespace around a non-whitespace delimiter is absorbed; two delimiters make an empty field" {
  run_lab "$PROBE" fields ' :' '  : a : : b :  '
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | busybox | mksh | yash | ksh93 | posh)
      [ "$output" = "4:<><a><><b>" ]
      ;;
    zsh | zsh-sh) [ "$output" = "5:<><a><><b><>" ] ;;
    *) false ;;
  esac
}

@test "an IFS assigned in a function leaks to the caller; a subshell and IFS=: read do not" {
  run_lab "$PROBE" leak
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "before: 2:<a><b:c>" ]
  [ "${lines[1]}" = "after_subshell: 2:<a><b:c>" ]
  [ "${lines[2]}" = "read: <x><y>" ]
  [ "${lines[3]}" = "after_read: 2:<a><b:c>" ]
  [ "${lines[4]}" = "after_leak: 2:<a b><c>" ]
}

@test "a leaked IFS=';' turns the arguments of eval \"\$*\" into separate commands" {
  run_lab "$PROBE" wrapper
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "default: marker" ]
  [ "${lines[1]}" = "leaked: |MARKER" ]
}

@test "\"\$*\" joins with the first IFS character, with nothing for a null IFS, with a space when unset" {
  run_lab "$PROBE" join
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "custom: <a;b c;d>" ]
  [ "${lines[1]}" = "null: <ab cd>" ]
  [ "${lines[2]}" = "unset: <a b c d>" ]
}

@test "read without -r lets a backslash escape the delimiter, so data can hide a separator" {
  run_lab "$PROBE" readesc
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = 'with_r: <a\><b:c>' ]
  [ "${lines[1]}" = "without_r: <a:b><c>" ]
}

@test "a delimiter inside a free-text field shifts every later field" {
  run_lab "$PROBE" record
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "two_vars: name=<bob> role=<admin,guest>" ]
  [ "${lines[1]}" = "three_vars: name=<bob> role=<admin> extra=<guest>" ]
}

@test "saving an unset IFS through a scalar and restoring it disables splitting" {
  run_lab "$PROBE" restore
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "naive_restore: 1:<a b>" ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | busybox | mksh | yash | posh)
      [ "${lines[1]}" = "exact_restore: 2:<a><b>" ]
      ;;
    ksh93)
      # ksh93u+ 2012 reports an unset IFS as set and empty, so the exact restore also fails there.
      # ksh93u+m 1.0.10 reports it as unset, so the exact restore works (measured in the shell matrix).
      case $(lab_shell_version) in
        *93u+m*) [ "${lines[1]}" = "exact_restore: 2:<a><b>" ] ;;
        *) [ "${lines[1]}" = "exact_restore: 1:<a b>" ] ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "an unknown mode is a usage error" {
  run_lab "$PROBE" no_such_mode
  [ "$status" -eq 1 ]
}
