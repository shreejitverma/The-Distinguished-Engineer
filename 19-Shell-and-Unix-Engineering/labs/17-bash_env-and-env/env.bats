#!/usr/bin/env bats
# Tests for BASH_ENV and ENV behavior across shell families:
# execution modes, parameter and command substitution expansion, PATH independence,
# privilege suppression, and option dynamics.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

# probe MODE: run probe.sh under the shell under test, invoking the shell under test in MODE.
probe() {
  lab_shell_words
  run_lab "$PROBE" "$1" "${LAB_SHELL_WORDS[@]}"
}

# startup_family: lab_shell_family, distinguishing bash invoked as sh (bash-sh)
startup_family() {
  local family
  family=$(lab_shell_family)
  lab_shell_words
  if [ "$family" = bash ] && [ "${LAB_SHELL_WORDS[0]##*/}" = sh ]; then
    family=bash-sh
  fi
  printf '%s\n' "$family"
}

@test "non-interactive shell sources BASH_ENV in bash, ignored by all POSIX shells" {
  probe noninteractive
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "body: main_ran" ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: BASH_ENV" ] ;;
    bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}

@test "interactive shell sources ENV in POSIX shells, ignored by native bash, native zsh, yash outside POSIX mode, and posh" {
  probe interactive
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "body: main_ran" ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    dash | mksh | ksh93 | busybox | zsh-sh | bash-sh)
      [ "${lines[0]}" = "trace: ENV" ]
      ;;
    bash | zsh | posh | yash) # yash reads ENV only in POSIX mode (-o posix or the name sh)
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}

@test "bash in POSIX mode sources ENV when interactive, unless shadowed by Apple bash" {
  probe posix-interactive
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "body: main_ran" ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    bash)
      case $(lab_shell_version) in
        3.2.57*) [ "${lines[0]}" = "trace: -" ] ;; # Apple bash 3.2 shadows posixly_correct in run_startup_files
        *) [ "${lines[0]}" = "trace: ENV" ] ;;
      esac
      ;;
    bash-sh | dash | mksh | ksh93 | busybox | zsh-sh)
      [ "${lines[0]}" = "trace: ENV" ]
      ;;
    zsh | posh | yash) # POSIXLY_CORRECT does not put yash in POSIX mode
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}

@test "POSIX mode non-interactive shells ignore both BASH_ENV and ENV" {
  probe posix-noninteractive
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "trace: -" ]
  [ "${lines[1]}" = "body: main_ran" ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    bash | bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}

@test "BASH_ENV undergoes parameter expansion and command substitution in bash" {
  probe cmdsub
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "body: main_ran" ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: CMDSUB" ] ;;
    bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}

@test "ENV undergoes parameter expansion and command substitution in interactive POSIX shells, but not in mksh" {
  probe cmdsub-env
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "body: main_ran" ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    dash | ksh93 | zsh-sh | bash-sh)
      [ "${lines[0]}" = "trace: CMDSUB_ENV" ]
      ;;
    busybox)
      # Debian's BusyBox ash leaves its interactive banner in the stdout buffer, and the
      # command substitution child flushes it into the trace file ahead of the tag.
      [[ ${lines[0]} == "trace: "*CMDSUB_ENV ]]
      ;;
    bash | zsh | posh | yash | mksh) # mksh uses the ENV value literally; yash is not in POSIX mode
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}

@test "errexit set -e is bypassed during BASH_ENV execution in bash" {
  probe errexit
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "body: before_false" ]
  [ "${lines[2]}" = "status: 1" ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: ENV_SURVIVED" ] ;;
    bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}

@test "BASH_ENV does not search PATH and resolves relative path against current directory" {
  probe cwd
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "body: main_ran" ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: CWD_INIT" ] ;;
    bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}

@test "privileged mode -p suppresses BASH_ENV and ENV across all supporting shells" {
  probe privileged
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash | bash-sh | ksh93 | mksh | zsh | zsh-sh)
      [ "${lines[0]}" = "trace: -" ]
      [ "${lines[1]}" = "body: main_ran" ]
      [ "${lines[2]}" = "status: 0" ]
      ;;
    dash)
      # Debian dash accepts -p, Alpine/macOS Homebrew dash rejects -p with status 2
      if [ "${lines[2]}" = "status: 0" ]; then
        [ "${lines[0]}" = "trace: -" ]
        [ "${lines[1]}" = "body: main_ran" ]
      else
        [ "${lines[2]}" = "status: 2" ]
      fi
      ;;
    posh)
      [ "${lines[2]}" = "status: 1" ]
      ;;
    busybox | yash)
      [ "${lines[2]}" = "status: 2" ]
      ;;
    *) false ;;
  esac
}

@test "restricted shell -r executes BASH_ENV before restrictions lock down" {
  probe restricted
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash)
      [ "${lines[0]}" = "trace: RESTRICTED_BASH_ENV" ]
      [ "${lines[1]}" = "body: main_ran" ]
      [ "${lines[2]}" = "status: 0" ]
      ;;
    bash-sh | ksh93 | mksh | zsh | zsh-sh)
      [ "${lines[0]}" = "trace: -" ]
      [ "${lines[1]}" = "body: main_ran" ]
      [ "${lines[2]}" = "status: 0" ]
      ;;
    dash | busybox | yash)
      [ "${lines[2]}" = "status: 2" ]
      ;;
    posh)
      [ "${lines[2]}" = "status: 1" ]
      ;;
    *) false ;;
  esac
}

@test "positional parameters and dollar zero in BASH_ENV differ by bash version" {
  probe params
  [ "$status" -eq 0 ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    bash)
      case $(lab_shell_version) in
        3.* | 4.*)
          # Older bash sets $0 to shell path and $# is 0 during startup file execution
          [[ ${lines[0]} == "trace: params: 0="*bash*" #=0" ]]
          ;;
        *)
          # Bash 5.0+ binds parameters before startup files and sets $0 to script name
          [[ ${lines[0]} == "trace: params: 0="*script.sh*" #=2" ]]
          ;;
      esac
      ;;
    bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}
