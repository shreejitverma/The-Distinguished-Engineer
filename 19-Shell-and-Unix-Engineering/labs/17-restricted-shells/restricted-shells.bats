#!/usr/bin/env bats
# Restricted shells: what `-r` and the `r` name prefix really enforce, shell by shell,
# and what they leave open. Every verdict is computed by attempt.sh from the real effect.

setup() {
  load ../lib/lab
  ATTEMPT="$BATS_TEST_DIRNAME/attempt.sh"
  DIR="$BATS_TEST_TMPDIR/fx"
  mkdir -p "$DIR/bin"
  printf 'LAB_DOT=ran\n' >"$DIR/lib.sh"
  printf 'cd / || exit 1\npwd\n' >"$DIR/bin/plain"
  printf '#!/bin/sh\ncd / || exit 1\npwd\n' >"$DIR/bin/hashbang"
  chmod +x "$DIR/bin/plain" "$DIR/bin/hashbang"
  PATH="$DIR/bin:$PATH"
  export PATH
}

# Every attempt that has a meaningful unrestricted control (set +r is not valid in a shell
# that has no restricted mode, so it has no universal control).
CONTROL_NAMES="cd path shellvar histfile ifs slash commandp exec redirect append read dot shescape envescape awkescape plain hashbang"

# expect_attempt MODE NAME WANT: run attempt.sh NAME under the shell under test (MODE is -r or
# empty) and compare its verdict; WANT may be allowed, blocked, or rejected (the shell refused -r).
expect_attempt() {
  local got
  rm -f "$DIR/out" "$DIR/app"
  if [ -n "$1" ]; then
    run_lab "$1" "$ATTEMPT" "$2" "$DIR"
  else
    run_lab "$ATTEMPT" "$2" "$DIR"
  fi
  got=$(printf '%s\n' "$output" | awk -F': ' -v n="$2" '$1 == n { v = $2 } END { print v }')
  if [ "$3" = rejected ]; then
    [ "$status" -ne 0 ] && [ -z "$got" ] && return 0
  elif [ "$got" = "$3" ]; then
    return 0
  fi
  printf 'attempt %s (mode "%s"): got "%s", want "%s"\n%s\n' "$2" "$1" "$got" "$3" "$output" >&2
  return 1
}

# expected_verdict NAME: the verdict the restricted shell under test must give.
# Sources: bash 5.3 and 3.2 measured here, bash-5.3 CHANGES for version differences, the zsh 5.9
# and ksh93u+m source and manuals, the mksh R59c manual and source; the mksh and ksh93u+m arms
# were checked by running attempt.sh by hand in the Alpine and Debian matrix images.
expected_verdict() {
  local name=$1 family version
  family=$(lab_shell_family)
  version=$(lab_shell_version)
  case $family in
    dash | yash | busybox | posh)
      echo rejected
      return 0
      ;;
    bash | zsh | zsh-sh | ksh93 | mksh) ;;
    *) return 1 ;;
  esac
  case $name in
    cd | path | shellvar | slash | redirect | append) echo blocked ;;
    read | shescape | envescape | awkescape | plain | hashbang) echo allowed ;;
    commandp)
      case $family in
        bash | zsh | ksh93 | mksh) echo blocked ;;
        zsh-sh) echo allowed ;; # POSIX_BUILTINS makes `command -p echo` run the builtin
        *) return 1 ;;
      esac
      ;;
    exec)
      case $family in
        bash | zsh | zsh-sh | ksh93) echo blocked ;;
        mksh) echo allowed ;; # mksh does not restrict exec; `exec echo` has no slash
        *) return 1 ;;
      esac
      ;;
    dot)
      case $family in
        bash | ksh93) echo blocked ;;
        zsh | zsh-sh | mksh) echo allowed ;;
        *) return 1 ;;
      esac
      ;;
    ifs)
      case $family in
        zsh | zsh-sh) echo blocked ;;
        bash | ksh93 | mksh) echo allowed ;;
        *) return 1 ;;
      esac
      ;;
    histfile)
      case $family in
        zsh | zsh-sh) echo blocked ;;
        ksh93) echo allowed ;;
        mksh)
          # mksh R59c source and Alpine's R59 2020/10/31 leave HISTFILE writable; Debian's
          # R59 2025/04/26 makes it read-only (measured by hand in the matrix images).
          case $version in
            *" R59 2020/"* | *" R5"[0-8]" "*) echo allowed ;;
            *) echo blocked ;;
          esac
          ;;
        bash)
          # HISTFILE became read-only in restricted shells in bash-5.1-alpha (CHANGES).
          case $version in
            [1-4].* | 5.0.*) echo allowed ;;
            *) echo blocked ;;
          esac
          ;;
        *) return 1 ;;
      esac
      ;;
    setplus)
      case $family in
        ksh93)
          # ksh93u+m 2020-06-10 (NEWS): set +r can no longer unset the restricted option.
          case $version in
            *93u+m*) echo blocked ;;
            *) echo allowed ;;
          esac
          ;;
        bash | zsh | zsh-sh | mksh) echo blocked ;;
        *) return 1 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}

# check_attempts NAME...: each restricted attempt matches the verdict matrix above.
check_attempts() {
  local name want
  for name in "$@"; do
    want=$(expected_verdict "$name")
    expect_attempt -r "$name" "$want"
  done
}

# link_as NAME: symlink the shell under test as $DIR/NAME and print the LAB_SHELL value that
# runs it with the same extra arguments (for example "--emulate sh").
link_as() {
  local target
  lab_shell_words
  target=$(command -v "${LAB_SHELL_WORDS[0]}")
  ln -s "$target" "$DIR/$1"
  printf '%s' "$DIR/$1"
  if [ "${#LAB_SHELL_WORDS[@]}" -gt 1 ]; then
    printf ' %s' "${LAB_SHELL_WORDS[@]:1}"
  fi
}

@test "control: without -r every attempt succeeds, so a blocked verdict means the restriction fired" {
  local name
  for name in $CONTROL_NAMES; do
    expect_attempt "" "$name" allowed
  done
}

@test "-r is accepted by bash, zsh, ksh93 and mksh and rejected by dash, yash, BusyBox ash and posh" {
  run_lab -r -c 'echo started'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = started ]
      ;;
    dash | yash | busybox | posh)
      [ "$status" -ne 0 ]
      case $output in
        *started*) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "restricted shells refuse cd and changes to SHELL and PATH" {
  check_attempts cd path shellvar
}

@test "restricted shells refuse a command name that contains a slash, and command -p" {
  check_attempts slash commandp
}

@test "restricted shells refuse output redirection but not reading a file" {
  check_attempts redirect append read
  # The refusal is real: nothing was created in the scratch directory.
  [ ! -e "$DIR/out" ]
}

@test "the protected-variable list differs by shell: HISTFILE and IFS" {
  check_attempts histfile ifs
}

@test "exec and the dot builtin are restricted in some shells and not in others" {
  check_attempts exec dot
}

@test "set +r lifts the restriction in macOS ksh93u+ 2012 and nowhere else" {
  check_attempts setplus
}

@test "an allowed command escapes: sh, env and awk start an unrestricted process" {
  check_attempts shescape envescape awkescape
}

@test "a script found on PATH runs unrestricted, with or without a #! line" {
  check_attempts plain hashbang
}

@test "the restricted flag shows in the option string only when -r is given" {
  run_lab -c 'echo "flags:$-"'
  [ "$status" -eq 0 ]
  case $output in
    *r*) false ;;
  esac
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | ksh93 | mksh)
      run_lab -r -c 'echo "flags:$-"'
      [ "$status" -eq 0 ]
      case $output in
        *r*) ;;
        *) false ;;
      esac
      ;;
    dash | yash | busybox | posh) ;;
    *) false ;;
  esac
}

@test "set -r inside a running shell turns the restrictions on where the shell allows it" {
  run_lab_c 'set -r; cd /; echo "cd=$?"'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | ksh93)
      case $output in
        *"cd=1"*) ;;
        *) false ;;
      esac
      ;;
    mksh)
      # mksh marks restricted as command-line only (sh_flags.opt, OF_CMDLINE).
      case $output in
        *"cd=1"*) false ;;
      esac
      ;;
    dash | yash | busybox | posh)
      # No restricted mode: either the shell rejects set -r or it ignores it, and cd works or the shell exits.
      case $output in
        *"cd=1"*) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "the restricted name prefix works in bash (exactly rbash), zsh, ksh93 and mksh" {
  local family shell
  family=$(lab_shell_family)
  case $family in
    bash)
      shell=$(link_as rbash)
      LAB_SHELL=$shell run_lab_c 'cd /; echo "cd=$?"'
      case $output in *"cd=1"*) ;; *) false ;; esac
      # Only the exact name rbash counts: other names, and other letter case, stay unrestricted.
      shell=$(link_as rbashx)
      LAB_SHELL=$shell run_lab_c 'cd /; echo "cd=$?"'
      [ "$output" = "cd=0" ]
      shell=$(link_as Rbash)
      LAB_SHELL=$shell run_lab_c 'cd /; echo "cd=$?"'
      [ "$output" = "cd=0" ]
      ;;
    zsh | zsh-sh)
      shell=$(link_as rzsh)
      LAB_SHELL=$shell run_lab_c 'cd /; echo "cd=$?"'
      case $output in *"cd=1"*) ;; *) false ;; esac
      ;;
    ksh93)
      shell=$(link_as rksh)
      LAB_SHELL=$shell run_lab_c 'cd /; echo "cd=$?"'
      case $output in *"cd=1"*) ;; *) false ;; esac
      ;;
    mksh)
      shell=$(link_as rmksh)
      LAB_SHELL=$shell run_lab_c 'cd /; echo "cd=$?"'
      case $output in *"cd=1"*) ;; *) false ;; esac
      ;;
    dash | yash | busybox | posh)
      # No restricted mode at all: -r is an illegal option.
      run_lab -r -c 'echo started'
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "bash runs BASH_ENV before the restriction starts; no other shell reads it" {
  local family
  family=$(lab_shell_family)
  printf 'cd / && echo "startup: cd worked"\n' >"$DIR/benv"
  export BASH_ENV="$DIR/benv"
  run_lab -c 'echo main'
  case $family in
    bash)
      [ "$output" = "$(printf 'startup: cd worked\nmain')" ]
      # With -r the same file still runs unrestricted, then cd fails in the command itself.
      run_lab -r -c 'cd /; echo "main:$?"'
      case $output in *"startup: cd worked"*) ;; *) false ;; esac
      case $output in *"main:1"*) ;; *) false ;; esac
      ;;
    zsh | zsh-sh | ksh93 | mksh | dash | yash | busybox | posh)
      [ "$output" = main ]
      ;;
    *) false ;;
  esac
}

@test "bash shopt restricted_shell reports the state and cannot be switched off" {
  case $(lab_shell_family) in
    bash)
      run_lab -r -c 'shopt -u restricted_shell; shopt -q restricted_shell && echo still-on; cd /; echo "cd=$?"'
      case $output in *still-on*) ;; *) false ;; esac
      case $output in *"cd=1"*) ;; *) false ;; esac
      ;;
    zsh | zsh-sh | ksh93 | mksh | dash | yash | busybox | posh)
      run_lab -c 'shopt -q restricted_shell'
      [ "$status" -eq 127 ]
      ;;
    *) false ;;
  esac
}

@test "zsh modules write files in restricted mode; the other shells have no such module" {
  case $(lab_shell_family) in
    zsh | zsh-sh)
      # zsh/mapfile turns an assignment into a file write, which -r does not intercept.
      # shellcheck disable=SC2016 # the expansion belongs to the shell under test
      run_lab -r -c 'zmodload zsh/mapfile && mapfile[$1/viamap]=written' x "$DIR"
      [ "$status" -eq 0 ]
      [ -f "$DIR/viamap" ]
      written=$(cat "$DIR/viamap")
      [ "$written" = written ]
      ;;
    bash | ksh93 | mksh | dash | yash | busybox | posh)
      run_lab -c 'zmodload zsh/mapfile'
      [ "$status" -eq 127 ]
      ;;
    *) false ;;
  esac
}
