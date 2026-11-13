#!/usr/bin/env bats
# Which startup files bash (and every other shell in the matrix) reads in each mode, and what bash
# takes from the environment at startup. Each test runs startup-probe.sh under the shell under test,
# for the shell under test; the probe prints three lines:
#   lines[0]  trace: the startup files that ran, in order ("-" for none)
#   lines[1]  body: what the -c command saw
#   lines[2]  status: the shell's exit status

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/startup-probe.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

# probe MODE: run the probe under the shell under test, starting the shell under test in MODE.
probe() {
  lab_shell_words
  run_lab "$PROBE" "$1" "${LAB_SHELL_WORDS[@]}"
}

# startup_family: lab_shell_family, with bash started under the name sh reported as bash-sh,
# because bash picks its startup files from the basename of argv[0] (shell.c, set_shell_name).
startup_family() {
  local family
  family=$(lab_shell_family)
  lab_shell_words
  if [ "$family" = bash ] && [ "${LAB_SHELL_WORDS[0]##*/}" = sh ]; then
    family=bash-sh
  fi
  printf '%s\n' "$family"
}

# is_login_trace: succeed when lines[0] is the trace of a non-interactive login shell (-l) for
# every family but bash: /etc/profile and ~/.profile, or the shell's own login files.
is_login_trace() {
  case $(startup_family) in
    bash-sh | dash | mksh | posh | ksh93 | busybox | zsh-sh) [ "${lines[0]}" = "trace: profile" ] ;;
    zsh) [ "${lines[0]}" = "trace: zshenv zprofile zlogin" ] ;;
    yash) [ "${lines[0]}" = "trace: yash_profile" ] ;;
    *) false ;;
  esac
}

@test "a non-interactive bash reads the file BASH_ENV names; as sh it reads none" {
  probe noninteractive
  [ "$status" -eq 0 ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: BASH_ENV" ] ;;
    zsh) [ "${lines[0]}" = "trace: zshenv" ] ;; # zsh reads ~/.zshenv in every mode
    bash-sh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash) [ "${lines[0]}" = "trace: -" ] ;;
    *) false ;;
  esac
}

@test "bash imports exported functions and SHELLOPTS from the environment" {
  probe noninteractive
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash | bash-sh) [[ ${lines[1]} == "body: ran fn=imported noglob=on "* ]] ;;
    zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      [[ ${lines[1]} == "body: ran fn=absent noglob=off "* ]]
      ;;
    *) false ;;
  esac
}

@test "bash unsets an inherited PS1 when it is not interactive" {
  probe noninteractive
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash | bash-sh) [[ ${lines[1]} == *" ps1=unset "* ]] ;;
    zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash) [[ ${lines[1]} == *" ps1=set "* ]] ;;
    *) false ;;
  esac
}

@test "SHLVL: bash, zsh, ksh93, and BusyBox count shell depth; dash, mksh, posh, and yash do not" {
  probe noninteractive
  [ "$status" -eq 0 ]
  local fresh=${lines[1]}
  probe socket-nested # the same command with SHLVL=1 inherited
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash | bash-sh | zsh | zsh-sh | ksh93 | busybox)
      [[ $fresh == *" shlvl=1 "* ]]
      [[ ${lines[1]} == *" shlvl=2 "* ]]
      ;;
    dash | mksh | posh | yash)
      [[ $fresh == *" shlvl=unset "* ]]
      [[ ${lines[1]} == *" shlvl=1 "* ]]
      ;;
    *) false ;;
  esac
}

@test "bash expands BASH_ENV, so a command substitution in it runs" {
  probe expand
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: cmdsub" ] ;;
    zsh) [ "${lines[0]}" = "trace: zshenv" ] ;;
    bash-sh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash) [ "${lines[0]}" = "trace: -" ] ;;
    *) false ;;
  esac
}

@test "bash 5.0 and later set the positional parameters before BASH_ENV runs" {
  local version shell_path
  version=$(lab_shell_version)
  lab_shell_words
  shell_path=$(command -v "${LAB_SHELL_WORDS[0]}")
  probe args
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash)
      case $version in
        [34].*) [ "${lines[0]}" = "trace: BASH_ENV(0=$shell_path,#=0,1=)" ] ;; # bound afterwards
        *) [ "${lines[0]}" = "trace: BASH_ENV(0=zero,#=2,1=a)" ] ;;
      esac
      ;;
    zsh) [ "${lines[0]}" = "trace: zshenv" ] ;;
    bash-sh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash) [ "${lines[0]}" = "trace: -" ] ;;
    *) false ;;
  esac
}

@test "startup files run with set -e off, and set -e applies to the command afterwards" {
  probe errexit
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "body: ran" ] # not-reached never printed: false ended the shell
  [ "${lines[2]}" = "status: 1" ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: BASH_ENV-after-false" ] ;;
    zsh) [ "${lines[0]}" = "trace: zshenv" ] ;;
    bash-sh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash) [ "${lines[0]}" = "trace: -" ] ;;
    *) false ;;
  esac
}

@test "an interactive bash reads ~/.bashrc and ignores ENV; as sh it reads ENV" {
  probe interactive
  [ "$status" -eq 0 ]
  [[ ${lines[1]} == *" interactive=yes cd=ok" ]]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: bashrc" ] ;;
    bash-sh | zsh-sh | dash | mksh | ksh93 | busybox) [ "${lines[0]}" = "trace: ENV" ] ;;
    zsh) [ "${lines[0]}" = "trace: zshenv zshrc" ] ;;
    yash) [ "${lines[0]}" = "trace: yashrc" ] ;; # yash reads ENV only in POSIX mode
    posh) [ "${lines[0]}" = "trace: -" ] ;;      # posh builds ENV support only with SILLY_FEATURES
    *) false ;;
  esac
}

@test "a login bash reads ~/.bash_profile, not ~/.profile, and still reads BASH_ENV" {
  probe login
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: bash_profile BASH_ENV" ] ;;
    bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      is_login_trace
      ;;
    *) false ;;
  esac
}

@test "a login bash falls back to ~/.profile when ~/.bash_profile and ~/.bash_login are missing" {
  probe fallback
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: profile BASH_ENV" ] ;;
    bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      is_login_trace
      ;;
    *) false ;;
  esac
}

@test "an empty ~/.bash_profile hides ~/.profile from a login bash" {
  probe empty
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: BASH_ENV" ] ;;
    bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      is_login_trace
      ;;
    *) false ;;
  esac
}

@test "an unreadable ~/.bash_profile is reported and still hides ~/.profile" {
  export STARTUP_PROBE_STDERR=1
  probe unreadable
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash)
      [ "${lines[0]}" = "trace: BASH_ENV" ]
      [[ $output == *"/home/.bash_profile: Permission denied"* ]]
      ;;
    bash-sh | zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      is_login_trace
      [[ $output != *"Permission denied"* ]]
      ;;
    *) false ;;
  esac
}

@test "a login bash reads ~/.bash_logout on exit; zsh skips .zlogout when not interactive" {
  probe logout
  [ "$status" -eq 0 ]
  [ "${lines[2]}" = "status: 0" ]
  case $(startup_family) in
    bash) [ "${lines[0]}" = "trace: bash_profile BASH_ENV bash_logout" ] ;;
    bash-sh) [ "${lines[0]}" = "trace: profile bash_logout" ] ;;
    zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      is_login_trace
      ;;
    *) false ;;
  esac
}

@test "argv[0] starting with - makes every shell a login shell; bash also needs a build option without -i" {
  probe exit # the same command under the shell's plain name: not a login shell
  [ "$status" -eq 0 ]
  local plain=${lines[0]}
  probe minus-name
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash)
      # ~/.bash_logout at exit shows bash took the "-" as a login mark in every build. Reading the
      # profile files as well needs NON_INTERACTIVE_LOGIN_SHELLS (config-top.h), a build option
      # with no other visible effect: Debian and nix define it; Alpine, Apple, and upstream do not.
      [ "$plain" = "trace: BASH_ENV" ]
      [ "${lines[0]}" = "trace: BASH_ENV bash_logout" ] \
        || [ "${lines[0]}" = "trace: bash_profile BASH_ENV bash_logout" ]
      ;;
    bash-sh)
      [ "$plain" = "trace: -" ]
      [ "${lines[0]}" = "trace: bash_logout" ] || [ "${lines[0]}" = "trace: profile bash_logout" ]
      ;;
    zsh)
      [ "$plain" = "trace: zshenv" ]
      is_login_trace
      ;;
    zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      [ "$plain" = "trace: -" ]
      is_login_trace
      ;;
    *) false ;;
  esac
}

@test "bash started as sh reads no file when not interactive and turns on posix mode after startup" {
  probe sh-noninteractive
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "trace: -" ]
  case $(startup_family) in
    bash | bash-sh) [[ ${lines[1]} == *" posix=yes "* ]] ;;
    zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash) [[ ${lines[1]} == *" posix=no "* ]] ;;
    *) false ;;
  esac
}

@test "bash started as sh reads ENV before it enters posix mode" {
  probe sh-interactive
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash | bash-sh)
      [ "${lines[0]}" = "trace: ENV(posix=no)" ]
      [[ ${lines[1]} == *" posix=yes "* ]]
      ;;
    zsh | zsh-sh | dash | mksh | ksh93 | busybox | yash)
      [ "${lines[0]}" = "trace: ENV(posix=no)" ]
      [[ ${lines[1]} == *" posix=no "* ]]
      ;;
    posh) [ "${lines[0]}" = "trace: -" ] ;;
    *) false ;;
  esac
}

@test "bash started as sh with -l reads /etc/profile and ~/.profile only; yash as sh reads none" {
  probe sh-login
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash | bash-sh)
      [ "${lines[0]}" = "trace: profile" ]
      [[ ${lines[1]} == *" posix=yes "* ]]
      ;;
    zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox) [ "${lines[0]}" = "trace: profile" ] ;;
    yash) [ "${lines[0]}" = "trace: -" ] ;; # yash.c reads a profile only when not posixly correct
    *) false ;;
  esac
}

@test "POSIXLY_CORRECT makes an interactive bash read ENV in posix mode; Apple's 3.2 reads ~/.bashrc" {
  local version machtype
  version=$(lab_shell_version)
  # shellcheck disable=SC2016 # MACHTYPE belongs to the shell under test
  run_lab_c 'printf "%s\n" "${MACHTYPE-}"'
  machtype=$output
  probe posix-interactive
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash)
      case $version:$machtype in
        # Apple's shell.c shadows posixly_correct with a local 0 for interactive startup.
        3.2.*:*-apple-darwin*) [ "${lines[0]}" = "trace: bashrc" ] ;;
        *) [ "${lines[0]}" = "trace: ENV(posix=yes)" ] ;;
      esac
      [[ ${lines[1]} == *" noglob=off "* ]] # posix mode rejects the inherited SHELLOPTS
      ;;
    bash-sh)
      [ "${lines[0]}" = "trace: ENV(posix=yes)" ]
      [[ ${lines[1]} == *" noglob=off "* ]]
      ;;
    zsh-sh | dash | mksh | ksh93 | busybox) [ "${lines[0]}" = "trace: ENV(posix=no)" ] ;;
    zsh) [ "${lines[0]}" = "trace: zshenv zshrc" ] ;;
    yash) [ "${lines[0]}" = "trace: yashrc" ] ;;
    posh) [ "${lines[0]}" = "trace: -" ] ;;
    *) false ;;
  esac
}

@test "POSIXLY_CORRECT makes a login bash read no profile file at all" {
  probe posix-login
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash | bash-sh) [ "${lines[0]}" = "trace: -" ] ;;
    zsh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      is_login_trace
      ;;
    *) false ;;
  esac
}

@test "bash -c reads ~/.bashrc instead of BASH_ENV when stdin is a socket, unless SHLVL says nested" {
  probe socket
  [ "$status" -eq 0 ]
  local top=${lines[0]}
  probe socket-nested
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash)
      [ "$top" = "trace: bashrc" ]
      [ "${lines[0]}" = "trace: BASH_ENV" ]
      ;;
    zsh)
      [ "$top" = "trace: zshenv" ]
      [ "${lines[0]}" = "trace: zshenv" ]
      ;;
    bash-sh | zsh-sh | dash | mksh | posh | ksh93 | busybox | yash)
      [ "$top" = "trace: -" ]
      [ "${lines[0]}" = "trace: -" ]
      ;;
    *) false ;;
  esac
}

@test "bash -p skips BASH_ENV and imports no functions or SHELLOPTS" {
  local dash_has_p=no
  run_lab -p -c ':'
  if [ "$status" -eq 0 ]; then
    dash_has_p=yes
  fi
  probe privileged
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash | bash-sh)
      [ "${lines[0]}" = "trace: -" ]
      [[ ${lines[1]} == "body: ran fn=absent noglob=off "* ]]
      [ "${lines[2]}" = "status: 0" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh) # zsh -p skips ~/.zshenv too
      [ "${lines[0]}" = "trace: -" ]
      [ "${lines[2]}" = "status: 0" ]
      ;;
    dash) # Debian's dash 0.5.12 accepts -p; Alpine's 0.5.12 and Homebrew's 0.5.13.5 reject it
      [ "${lines[0]}" = "trace: -" ]
      if [ "$dash_has_p" = yes ]; then
        [ "${lines[2]}" = "status: 0" ]
      else
        [ "${lines[1]}" = "body: -" ]
        [ "${lines[2]}" = "status: 2" ]
      fi
      ;;
    busybox | yash)
      [ "${lines[1]}" = "body: -" ]
      [ "${lines[2]}" = "status: 2" ]
      ;;
    posh)
      [ "${lines[1]}" = "body: -" ]
      [ "${lines[2]}" = "status: 1" ]
      ;;
    *) false ;;
  esac
}

@test "bash -r runs BASH_ENV unrestricted, then refuses cd" {
  probe restricted
  [ "$status" -eq 0 ]
  case $(startup_family) in
    bash)
      [ "${lines[0]}" = "trace: BASH_ENV-cd-ok" ]
      [[ ${lines[1]} == *" cd=denied" ]]
      ;;
    zsh)
      [ "${lines[0]}" = "trace: zshenv" ]
      [[ ${lines[1]} == *" cd=denied" ]]
      ;;
    bash-sh | zsh-sh | ksh93 | mksh)
      [ "${lines[0]}" = "trace: -" ]
      [[ ${lines[1]} == *" cd=denied" ]]
      ;;
    dash | busybox | yash)
      [ "${lines[1]}" = "body: -" ]
      [ "${lines[2]}" = "status: 2" ]
      ;;
    posh)
      [ "${lines[1]}" = "body: -" ]
      [ "${lines[2]}" = "status: 1" ]
      ;;
    *) false ;;
  esac
}
