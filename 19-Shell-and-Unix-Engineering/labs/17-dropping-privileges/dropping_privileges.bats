#!/usr/bin/env bats
# Tests for privilege-dropping mechanics, order of operations, credential verification,
# descriptor hygiene, environment scrubbing, and shell privileged mode (-p) behavior.

setup() {
  load ../lib/lab
  T="$BATS_TEST_TMPDIR"
  DROPPER="$BATS_TEST_DIRNAME/dropper.sh"
  DROPPROBE="$T/dropprobe"

  # Compile C probe with compiler from environment
  "${CC:-cc}" -Wall -Wextra -Werror -o "$DROPPROBE" "$BATS_TEST_DIRNAME/dropprobe.c"
}

@test "dropprobe compiles and reports real and effective user IDs" {
  run "$DROPPROBE" ids
  [ "$status" -eq 0 ]
  [[ "$output" =~ ruid=[0-9]+ ]]
  [[ "$output" =~ euid=[0-9]+ ]]
  [[ "$output" =~ groups=[0-9]+ ]]
}

@test "unprivileged process cannot regain root privileges via setuid or seteuid" {
  run "$DROPPROBE" try-root
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "setuid(0): ret=-1 errno=EPERM" ]
  [ "${lines[1]}" = "seteuid(0): ret=-1 errno=EPERM" ]
  [ "${lines[2]}" = "setgid(0): ret=-1 errno=EPERM" ]
}

@test "setgroups fails with EPERM for unprivileged caller proving drop order requirement" {
  run "$DROPPROBE" order-check
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "setgroups(0): ret=-1 errno=EPERM" ]
}

@test "unclosed file descriptors leak across exec without O_CLOEXEC" {
  run "$DROPPER" fdleak "$DROPPROBE"
  [ "$status" -eq 0 ]
  [ "$output" = "fd 3: open flags=0" ]
}

@test "explicit descriptor closure prevents leakage across privilege boundary" {
  run "$DROPPER" fdclose "$DROPPROBE"
  [ "$status" -eq 1 ]
  [ "$output" = "fd 3: closed errno=EBADF" ]
}

@test "environment scrubbing strips hostile variables before unprivileged exec" {
  # shellcheck disable=SC2030,SC2031 # intentional export to test scrubbing wrapper
  export BASH_ENV="$T/hostile.sh"
  # shellcheck disable=SC2030,SC2031
  export LD_PRELOAD="/path/to/hostile.so"
  # shellcheck disable=SC2030,SC2031
  export SECRET_KEY="SUPER-SECRET-VALUE"

  run "$DROPPER" scrub env
  [ "$status" -eq 0 ]
  # Assert dangerous and leaked variables are absent
  [[ "$output" != *"BASH_ENV="* ]]
  [[ "$output" != *"LD_PRELOAD="* ]]
  [[ "$output" != *"SECRET_KEY="* ]]
  # Assert safe path was injected
  [[ "$output" =~ PATH=/usr/bin:/bin ]]
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

@test "shell privileged mode suppresses BASH_ENV in bash and preserves family invariants" {
  printf 'printf "PAYLOAD_EXECUTED\\n"\n' >"$T/payload.sh"
  chmod +x "$T/payload.sh"

  lab_shell_words
  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c '
    payload="$1"
    shift
    BASH_ENV="$payload" ENV="$payload" "$@" -c "printf \"%s\n\" main"
  ' sh "$T/payload.sh" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  normal_output="$output"

  # shellcheck disable=SC2016 # expressions belong to the shell under test
  run_lab_c '
    payload="$1"
    shift
    BASH_ENV="$payload" ENV="$payload" "$@" -p -c "printf \"%s\n\" main"
  ' sh "$T/payload.sh" "${LAB_SHELL_WORDS[@]}"
  priv_status="$status"
  priv_output="$output"

  case $(startup_family) in
    bash)
      [ "$normal_output" = "PAYLOAD_EXECUTED"$'\n'"main" ]
      [ "$priv_status" -eq 0 ]
      [ "$priv_output" = "main" ]
      ;;
    bash-sh | zsh | zsh-sh)
      # Native zsh, zsh --emulate sh, and bash in sh mode ignore BASH_ENV in non-interactive scripts
      [ "$normal_output" = "main" ]
      [ "$priv_status" -eq 0 ]
      [ "$priv_output" = "main" ]
      ;;
    dash)
      # Upstream dash (Alpine) rejects -p with status 2; Debian's privmode patch accepts it.
      # Neither reads BASH_ENV, and ENV is read only by interactive shells.
      [ "$normal_output" = "main" ]
      if "${LAB_SHELL_WORDS[@]}" -p -c ':' >/dev/null 2>&1; then
        [ "$priv_status" -eq 0 ]
        [ "$priv_output" = "main" ]
      else
        [ "$priv_status" -eq 2 ]
        [[ "$priv_output" == *"Illegal option -p"* ]]
      fi
      ;;
    busybox | posh | yash)
      # These shells reject -p outright, so the payload never runs and "main" is never printed.
      [ "$normal_output" = "main" ]
      [ "$priv_status" -ne 0 ]
      [[ "$priv_output" != *main* ]]
      [[ "$priv_output" != *PAYLOAD_EXECUTED* ]]
      ;;
    mksh | ksh93)
      [ "$normal_output" = "main" ]
      [ "$priv_status" -eq 0 ]
      [ "$priv_output" = "main" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "turning off privileged mode with set +p succeeds in supporting shells" {
  # The subshell keeps a special-builtin error (dash exits with status 2) from ending the probe.
  run_lab_c '(set +p) 2>/dev/null && echo ok || echo unsupported'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | ksh93 | mksh | zsh | zsh-sh)
      [ "$output" = "ok" ]
      ;;
    dash | busybox | posh | yash)
      # `set +p` works exactly when the shell accepts -p at invocation (only Debian's dash does)
      lab_shell_words
      if "${LAB_SHELL_WORDS[@]}" -p -c ':' >/dev/null 2>&1; then
        [ "$output" = "ok" ]
      else
        [ "$output" = "unsupported" ]
      fi
      ;;
    *)
      false
      ;;
  esac
}

@test "kernel PR_SET_NO_NEW_PRIVS status reports active on Linux or unsupported on macOS" {
  run "$DROPPROBE" nonewprivs
  [ "$status" -eq 0 ]
  case "$(uname -s)" in
    Linux)
      [ "$output" = "nonewprivs: active=1" ]
      ;;
    Darwin | *)
      [ "$output" = "nonewprivs: unsupported" ]
      ;;
  esac
}
