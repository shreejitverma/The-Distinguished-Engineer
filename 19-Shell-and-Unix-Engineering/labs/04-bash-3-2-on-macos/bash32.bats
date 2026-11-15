#!/usr/bin/env bats
# Tests for the bash 3.2 lab: which features each bash release has, and the silent and fatal
# ways a bash 4+ script misbehaves on the bash 3.2.57 that macOS ships as /bin/bash and /bin/sh.
# Every test asserts the exact result for the bash release under test (major, minor, patch from
# BASH_VERSION) and "not-bash" for every other family.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  GUARDED="$BATS_TEST_DIRNAME/guarded-assoc.sh"
  FAMILY=$(lab_shell_family)
  BV_NUM=0
  BV_PATCH=0
  if [ "$FAMILY" = bash ]; then
    local v rest
    v=$(lab_shell_version) # for example 3.2.57(1)-release
    v=${v%%(*}
    rest=${v#*.}
    BV_NUM=$((${v%%.*} * 100 + ${rest%%.*}))
    BV_PATCH=${rest#*.}
    BV_PATCH=${BV_PATCH%%[!0-9]*}
  fi
}

# patched_at P32 P40 P41 P42 P43: succeed if the bash under test carries the fix that shipped as
# bash32-P32, bash40-P40, bash41-P41, bash42-P42, or bash43-P43; every bash >= 4.4 has it.
patched_at() {
  case $BV_NUM in
    302) [ "$BV_PATCH" -ge "$1" ] ;;
    400) [ "$BV_PATCH" -ge "$2" ] ;;
    401) [ "$BV_PATCH" -ge "$3" ] ;;
    402) [ "$BV_PATCH" -ge "$4" ] ;;
    403) [ "$BV_PATCH" -ge "$5" ] ;;
    *) [ "$BV_NUM" -ge 404 ] ;;
  esac
}

assert_not_bash() {
  [ "$status" -eq 3 ]
  [ "$output" = not-bash ]
}

@test "features: each bash release has exactly the features its NEWS entry lists" {
  # name:release that introduced it (major*100 + minor), from the bash NEWS file.
  local table='regex:300 pipefail:300 appendop:301 printfv:301 comsubparen:300
    assoc:400 mapfile:400 casemod:400 declarel:400 fallthrough:400 globstar:400 pipeamp:400
    appendall:400 zeropad:400 readfrac:400 bashpid:400 comsubcase:400
    fdvar:401 printfvarr:401 readN:401
    uescape:402 testv:402 negsub:402 neglen:402 printfT:402 lastpipe:402 declareg:402
    nameref:403 waitn:403
    atQ:404 nounsetarr:404 printfvempty:404 inherit:404 localdash:404
    epoch:500 srandom:501 waitp:501 funsub:503'
  run_lab "$PROBE" features
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      local expected="" entry
      for entry in $table; do
        if [ "$BV_NUM" -ge "${entry#*:}" ]; then
          expected+="${entry%%:*}=yes"$'\n'
        else
          expected+="${entry%%:*}=no"$'\n'
        fi
      done
      diff <(printf '%s' "$expected") <(printf '%s\n' "$output")
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "assoc: bash 3.2 rejects declare -A, then folds every string key into index 0" {
  run_lab "$PROBE" assoc
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      if [ "$BV_NUM" -ge 400 ]; then
        [ "$output" = "foo=1 bar=2 count=2" ]
      else
        [ "$output" = "foo=2 bar=2 count=1" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "guard: a version check that bash 3.2 can parse refuses to run the bash 4 body" {
  run_lab "$GUARDED" a b a
  case $FAMILY in
    bash)
      if [ "$BV_NUM" -ge 400 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "a=2"$'\n'"b=1" ]
      else
        [ "$status" -eq 2 ]
        [[ $output == "guarded-assoc.sh: needs bash >= 4.0, running 3."* ]]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -eq 2 ]
      [ "$output" = "guarded-assoc.sh: needs bash >= 4.0, running a non-bash shell" ]
      ;;
    *) false ;;
  esac
}

@test "nounset: an empty array under set -u ends the script before bash 4.4" {
  run_lab "$PROBE" nounset
  case $FAMILY in
    bash)
      if [ "$BV_NUM" -ge 404 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "<> reached" ]
      else
        [ "$status" -eq 1 ]
        [[ $output == *"a[@]: unbound variable"* ]]
        [[ $output != *reached* ]]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "nounset-idiom: the plus-guarded expansion gives 0 and 2 arguments in every release" {
  run_lab "$PROBE" nounset-idiom
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      [ "$output" = "empty=0 two=2" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "fd-fatal: exec {fd}> makes bash 3.2 look up a command named {fd} and exit 127" {
  run_lab "$PROBE" fd-fatal
  case $FAMILY in
    bash)
      if [ "$BV_NUM" -ge 401 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "survived fd>=10" ]
      else
        [ "$status" -eq 127 ]
        [[ $output == *"exec: {fd}: not found"* ]]
        [[ $output != *survived* ]]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "echo-posix: Apple's bash 3.2 echo expands backslashes and rejects -n in POSIX mode" {
  # shellcheck disable=SC2016 # MACHTYPE belongs to the shell under test
  run_lab_c 'printf "%s\n" "${MACHTYPE-}"'
  local machtype=$output
  run_lab "$PROBE" echo-posix
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      if [ "$BV_NUM" -eq 302 ] && [[ $machtype == *-apple-darwin* ]]; then
        [ "$output" = "a"$'\n'"b"$'\n'"-n x" ]
      else
        [ "$output" = 'a\nb'$'\n'"x" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "export-func: exported functions travel as BASH_FUNC_name%% after the 2014 patches" {
  run_lab "$PROBE" export-func
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      if patched_at 54 41 14 50 27; then
        [ "$output" = 'encoding=BASH_FUNC_f%%' ]
      else
        [ "$output" = 'encoding=f' ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "shellshock: a patched bash ignores code after a function definition in the environment" {
  run_lab "$PROBE" shellshock
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      if patched_at 52 39 12 48 25; then
        [ "$output" = body ]
      else
        [ "$output" = "VULNERABLE"$'\n'"body" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "usage: an unknown mode is an error in every shell" {
  run_lab "$PROBE" no-such-mode
  case $FAMILY in
    bash)
      [ "$status" -eq 64 ]
      [[ $output == usage:* ]]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}
