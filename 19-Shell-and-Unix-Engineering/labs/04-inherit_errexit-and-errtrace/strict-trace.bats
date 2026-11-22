#!/usr/bin/env bats
# strict-trace.sh: set -Eeuo pipefail plus inherit_errexit, with an ERR trap that prints a stack trace.

setup() {
  load ../lib/lab
  SCRIPT="$BATS_TEST_DIRNAME/strict-trace.sh"
}

# needs_bash44: succeed when the shell under test is bash 4.4 or later; otherwise check the guard.
needs_bash44() {
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        [123].* | 4.[0-3].*) ;;
        *) return 0 ;;
      esac
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) ;;
    *) false ;;
  esac
  run_lab "$SCRIPT" nested
  [ "$status" -eq 2 ]
  [ "$output" = "strict-trace.sh: needs bash 4.4 or later" ]
  return 1
}

@test "nested: errtrace carries the trap into read_config, and -e stops the script after one trace" {
  needs_bash44 || return 0
  run_lab "$SCRIPT" nested
  [ "$status" -eq 1 ]
  [ "${lines[0]}" = "ERR status=1 subshell=0 command=false" ]
  [ "${lines[1]}" = "  at read_config line 32" ]
  [ "${lines[2]}" = "  at load line 37" ]
  [ "${lines[3]}" = "  at main line 43" ]
  [ "${#lines[@]}" -eq 4 ]
}

@test "comsub: the child traces and exits at false, then the failed assignment traces again in the parent" {
  needs_bash44 || return 0
  run_lab "$SCRIPT" comsub
  [ "$status" -eq 1 ]
  [ "${lines[0]}" = "ERR status=1 subshell=1 command=false" ]
  [ "${lines[1]}" = "  at read_config line 32" ]
  [ "${lines[2]}" = "  at load line 37" ]
  [ "${lines[3]}" = "  at main line 46" ]
  # shellcheck disable=SC2016 # BASH_COMMAND holds the unexpanded source text
  [ "${lines[4]}" = 'ERR status=1 subshell=0 command=value=$(load)' ]
  [ "${lines[5]}" = "  at main line 46" ]
  [ "${#lines[@]}" -eq 6 ]
}

@test "masked: the substitution stops and traces, but echo discards its status and the script runs on" {
  needs_bash44 || return 0
  run_lab "$SCRIPT" masked
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ERR status=1 subshell=1 command=false" ]
  [ "${lines[3]}" = "  at main line 50" ]
  [ "${lines[4]}" = "masked: []" ]
  [ "${lines[5]}" = "end of script" ]
  [ "${#lines[@]}" -eq 6 ]
}

@test "an unknown mode prints usage and exits 64" {
  needs_bash44 || return 0
  run_lab "$SCRIPT" bogus
  [ "$status" -eq 64 ]
  [ "$output" = "usage: strict-trace.sh nested|comsub|masked" ]
}
