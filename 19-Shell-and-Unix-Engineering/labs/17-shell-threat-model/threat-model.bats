#!/usr/bin/env bats
# Shell threat model: every input a script inherits across execve is attacker-controlled
# when the caller is. These tests feed hostile environment variables, a hostile umask,
# a hostile signal disposition, and a hostile PATH to innocent scripts, and show which
# inputs a script can reset in its prologue and which run before its first line.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  T="$BATS_TEST_TMPDIR"
  mkdir -p "$T/home" "$T/zd" "$T/app/work" "$T/evil/work" "$T/evil-bin"
  printf 'echo HOOK-startup-file >&2\n' >"$T/hook.sh"
  cp "$T/hook.sh" "$T/zd/.zshenv"
  printf '#!/bin/sh\nprintf "HIJACKED\\n"\n' >"$T/evil-bin/id"
  chmod +x "$T/evil-bin/id"
  lab_shell_words
  # env -i clears PATH, so the clean-boundary test needs the shell's absolute path.
  SHELL_ABS=$(command -v "${LAB_SHELL_WORDS[0]}")
  FAMILY=$(lab_shell_family)
}

# bash_mode: print posix when the shell under test is bash in POSIX mode (for example
# macOS /bin/sh), native for bash otherwise, and n/a for every other family.
bash_mode() {
  if [ "$FAMILY" != bash ]; then
    echo n/a
    return
  fi
  # shellcheck disable=SC2016 # SHELLOPTS belongs to the shell under test
  run_lab_c 'case :${SHELLOPTS-}: in *:posix:*) echo posix ;; *) echo native ;; esac'
  printf '%s\n' "$output"
}

@test "a clean environment runs the victim and nothing else" {
  run_lab "$LAB/with-env.sh" HOME="$T/home" "${LAB_SHELL_WORDS[@]}" "$LAB/victim.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "body y" ]
}

@test "BASH_ENV runs a file before the script only in bash outside POSIX mode" {
  mode=$(bash_mode)
  run_lab "$LAB/with-env.sh" HOME="$T/home" BASH_ENV="$T/hook.sh" \
    "${LAB_SHELL_WORDS[@]}" "$LAB/victim.sh"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      case $mode in
        native) [ "$output" = "HOOK-startup-file
body y" ] ;;
        posix) [ "$output" = "body y" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = "body y" ] ;;
    *) false ;;
  esac
}

@test "ENV is ignored by every non-interactive shell" {
  run_lab "$LAB/with-env.sh" HOME="$T/home" ENV="$T/hook.sh" \
    "${LAB_SHELL_WORDS[@]}" "$LAB/victim.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "body y" ]
}

@test "ZDOTDIR makes native zsh source an attacker's .zshenv before the script" {
  run_lab "$LAB/with-env.sh" HOME="$T/home" ZDOTDIR="$T/zd" \
    "${LAB_SHELL_WORDS[@]}" "$LAB/victim.sh"
  [ "$status" -eq 0 ]
  case $FAMILY in
    zsh) [ "$output" = "HOOK-startup-file
body y" ] ;;
    zsh-sh | bash | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = "body y" ] ;;
    *) false ;;
  esac
}

@test "an exported bash function named like a utility shadows it, even in POSIX mode" {
  run_lab "$LAB/with-env.sh" HOME="$T/home" 'BASH_FUNC_basename%%=() { echo HOOK-function; }' \
    "${LAB_SHELL_WORDS[@]}" "$LAB/victim.sh"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "body HOOK-function" ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = "body y" ] ;;
    *) false ;;
  esac
}

@test "SHELLOPTS=xtrace with a hostile PS4 runs code in bash before every command" {
  # shellcheck disable=SC2016 # PS4 is expanded by the shell under test
  run_lab "$LAB/with-env.sh" HOME="$T/home" SHELLOPTS=xtrace 'PS4=$(echo HOOK-ps4 >&2)' \
    "${LAB_SHELL_WORDS[@]}" "$LAB/victim.sh"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      # The lab runs as an ordinary user; bash 4.4 and later ignore PS4 from the environment as root.
      case $output in
        HOOK-ps4*"body y") ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = "body y" ] ;;
    *) false ;;
  esac
}

@test "privileged mode -p disables the environment hooks in bash and zsh" {
  # shellcheck disable=SC2016 # PS4 is expanded by the shell under test
  run_lab "$LAB/with-env.sh" HOME="$T/home" BASH_ENV="$T/hook.sh" ZDOTDIR="$T/zd" \
    'BASH_FUNC_basename%%=() { echo HOOK-function; }' SHELLOPTS=xtrace 'PS4=$(echo HOOK-ps4 >&2)' \
    "${LAB_SHELL_WORDS[@]}" -p "$LAB/victim.sh"
  case $FAMILY in
    bash | zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "body y" ]
      ;;
    dash | ksh93 | mksh | yash | posh | busybox)
      # These shells honor none of the hooks, so -p (where they accept it) changes nothing visible;
      # the assertion is that no hook text appears whatever -p does.
      case $output in
        *HOOK*) false ;;
        *) ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "a clean boundary (env -i) removes every startup hook in every shell" {
  # The second hop is env(1) itself, not with-env.sh: a #!/bin/sh launcher started inside the
  # hostile environment is a victim too (macOS /bin/sh is bash 3.2 and honors SHELLOPTS and PS4).
  # shellcheck disable=SC2016 # PS4 would be expanded by the shell under test
  run_lab "$LAB/with-env.sh" BASH_ENV="$T/hook.sh" ZDOTDIR="$T/zd" ENV="$T/hook.sh" \
    'BASH_FUNC_basename%%=() { echo HOOK-function; }' SHELLOPTS=xtrace 'PS4=$(echo HOOK-ps4 >&2)' \
    env -i PATH=/usr/bin:/bin HOME="$T/home" \
    "$SHELL_ABS" "${LAB_SHELL_WORDS[@]:1}" "$LAB/victim.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "body y" ]
}

@test "an inherited IFS is reset at startup, except by posh" {
  run_lab "$LAB/with-env.sh" HOME="$T/home" IFS=: "${LAB_SHELL_WORDS[@]}" "$LAB/ambient.sh" ifs
  [ "$status" -eq 0 ]
  case $FAMILY in
    posh) [ "$output" = "ifs=inherited fields=2" ] ;;
    bash | zsh | zsh-sh | dash | ksh93 | mksh | yash | busybox) [ "$output" = "ifs=reset fields=1" ] ;;
    *) false ;;
  esac
}

@test "an inherited CDPATH moves cd and writes the new directory to stdout" {
  cd "$T/app"
  run_lab "$LAB/with-env.sh" HOME="$T/home" CDPATH="$T/evil" \
    "${LAB_SHELL_WORDS[@]}" "$LAB/ambient.sh" cdpath
  [ "$status" -eq 0 ]
  case $FAMILY in
    # Native zsh tries ./work first unless "." is in cdpath (zsh manual, cd builtin).
    zsh) [ "$output" = "landed=app printed=no" ] ;;
    bash | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = "landed=evil printed=yes" ] ;;
    *) false ;;
  esac
}

@test "without CDPATH, cd work stays in the current directory" {
  cd "$T/app"
  run_lab "$LAB/with-env.sh" HOME="$T/home" "${LAB_SHELL_WORDS[@]}" "$LAB/ambient.sh" cdpath
  [ "$status" -eq 0 ]
  [ "$output" = "landed=app printed=no" ]
}

@test "an inherited umask decides the mode of a file the script creates" {
  umask 077
  run_lab "$LAB/ambient.sh" umask "$T/private"
  [ "$status" -eq 0 ]
  [ "$output" = "mode=-rw-------" ]
  umask 000
  run_lab "$LAB/ambient.sh" umask "$T/exposed"
  umask 077
  [ "$status" -eq 0 ]
  [ "$output" = "mode=-rw-rw-rw-" ]
}

@test "a signal ignored on entry cannot be trapped, except in zsh" {
  run_lab "$LAB/ambient.sh" trap
  [ "$status" -eq 0 ]
  [ "$output" = "trap=ran" ]
  trap '' USR1
  run_lab "$LAB/ambient.sh" trap
  trap - USR1
  [ "$status" -eq 0 ]
  case $FAMILY in
    zsh | zsh-sh) [ "$output" = "trap=ran" ] ;;
    bash | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = "trap=ignored" ] ;;
    *) false ;;
  esac
}

@test "an inherited PATH chooses which program a bare command name runs" {
  run_lab "$LAB/with-env.sh" HOME="$T/home" PATH="$T/evil-bin:$PATH" \
    "${LAB_SHELL_WORDS[@]}" "$LAB/ambient.sh" path
  [ "$status" -eq 0 ]
  [ "$output" = "id=HIJACKED" ]
}

@test "the hardened prologue neutralizes every input a script can reset" {
  me=$(/usr/bin/id -un)
  cd "$T/app"
  umask 000
  run_lab "$LAB/with-env.sh" HOME="$T/home" IFS=: CDPATH="$T/evil" PATH="$T/evil-bin:$PATH" \
    'BASH_FUNC_basename%%=() { echo HOOK-function; }' \
    "${LAB_SHELL_WORDS[@]}" "$LAB/hardened.sh" "$T/secret"
  umask 077
  [ "$status" -eq 0 ]
  [ "$output" = "fields=1
landed=app printed=no
mode=-rw-------
id=$me
basename=y" ]
}
