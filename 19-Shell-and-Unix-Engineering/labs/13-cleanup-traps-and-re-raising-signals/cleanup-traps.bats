#!/usr/bin/env bats
# Tests for Cleanup Traps and Re-Raising Signals.
# A shell's $? cannot distinguish "exited 143" from "killed by SIGTERM", so every
# script under test is started by sigobserve (a small C program), which prints
# the kernel's wait status. The shell under test runs `exec "$@"` through
# run_lab_c to become sigobserve, and sigobserve starts the shell under test
# (LAB_SHELL) on the script, so the matrix still swaps the shell.

setup() {
  load ../lib/lab
  TEMPLATE="$BATS_TEST_DIRNAME/cleanup-template.sh"
  PROBE="$BATS_TEST_DIRNAME/reraise-probe.sh"
  LOOP="$BATS_TEST_DIRNAME/wce-loop.sh"
  OBS="$BATS_TEST_TMPDIR/sigobserve"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -o "$OBS" "$BATS_TEST_DIRNAME/sigobserve.c"
  W="$BATS_TEST_TMPDIR/work"
  mkdir "$W"
}

teardown() {
  # Nothing a test started may outlive it.
  if [ -n "${worker-}" ]; then
    kill "$worker" 2>/dev/null || true
  fi
}

# observe [SIGOBSERVE-OPTION...] -- SCRIPT [ARG...]
observe() {
  local opts=()
  while [ "$1" != -- ]; do
    opts+=("$1")
    shift
  done
  shift
  lab_shell_words
  # shellcheck disable=SC2016 # "$@" belongs to the shell under test
  run_lab_c 'exec "$@"' sh "$OBS" "${opts[@]}" -- "${LAB_SHELL_WORDS[@]}" "$@"
}

# expect_last TEXT: the last output line (sigobserve's verdict) is TEXT.
expect_last() {
  local got=${lines[${#lines[@]} - 1]}
  [ "$got" = "$1" ]
}

# expect_count REGEX N: exactly N output lines match REGEX.
expect_count() {
  local n
  n=$(printf '%s\n' "$output" | grep -c -- "$1") || true
  [ "$n" -eq "$2" ]
}

# Read "SCRATCH WORKER" from the template's ready file.
read_ready() {
  read -r scratch worker <"$W/ready"
  [ -n "$scratch" ]
  [ -n "$worker" ]
}

# The template must have removed its directory, reaped its worker, and cleaned once.
assert_cleaned_once() {
  read_ready
  [ ! -e "$scratch" ]
  if kill -0 "$worker" 2>/dev/null; then
    return 1 # the worker outlived the script
  fi
  expect_count '^cleanup: removed ' 1
}

@test "template: a normal exit cleans up once and exits 0" {
  observe -- "$TEMPLATE" "$W" "$W/ready" 1
  [ "$status" -eq 0 ]
  expect_last "exited 0"
  assert_cleaned_once
}

@test "template: exit status 3 survives the EXIT trap" {
  observe -- "$TEMPLATE" "$W" "$W/ready" 1 3
  expect_last "exited 3"
  assert_cleaned_once
}

@test "template: SIGTERM cleans up once and re-raises; zsh and mksh fall back to exit 143" {
  observe -k TERM -r "$W/ready" -- "$TEMPLATE" "$W" "$W/ready" 5
  assert_cleaned_once
  case $(lab_shell_family) in
    bash | dash | ksh93 | yash | posh | busybox) expect_last "signaled TERM" ;;
    zsh | zsh-sh | mksh) expect_last "exited 143" ;;
    *) false ;;
  esac
}

@test "template: SIGINT cleans up once and re-raises; zsh and mksh fall back to exit 130" {
  observe -k INT -r "$W/ready" -- "$TEMPLATE" "$W" "$W/ready" 5
  assert_cleaned_once
  case $(lab_shell_family) in
    bash | dash | ksh93 | yash | posh | busybox) expect_last "signaled INT" ;;
    zsh | zsh-sh | mksh) expect_last "exited 130" ;;
    *) false ;;
  esac
}

@test "template: SIGHUP cleans up once and re-raises; zsh, mksh, and posh fall back to exit 129" {
  observe -k HUP -r "$W/ready" -- "$TEMPLATE" "$W" "$W/ready" 5
  assert_cleaned_once
  case $(lab_shell_family) in
    bash | dash | ksh93 | yash | busybox) expect_last "signaled HUP" ;;
    zsh | zsh-sh | mksh | posh) expect_last "exited 129" ;;
    *) false ;;
  esac
}

@test "template: SIGINT ignored on entry cannot be trapped, except in zsh" {
  observe -i INT -k INT -r "$W/ready" -- "$TEMPLATE" "$W" "$W/ready" 3
  assert_cleaned_once
  case $(lab_shell_family) in
    # The trap was never installed: the INT is discarded and the script finishes normally.
    bash | dash | ksh93 | mksh | yash | posh | busybox) expect_last "exited 0" ;;
    # zsh installs the trap anyway; its re-raise lands on SIG_IGN, so the fallback exits 130.
    zsh | zsh-sh) expect_last "exited 130" ;;
    *) false ;;
  esac
}

@test "naive trap 'cleanup; exit 143' reports an exit, not a death by signal" {
  observe -k TERM -r "$W/ready" -- "$PROBE" naive "$W/ready"
  expect_count '^cleanup$' 1
  expect_last "exited 143"
}

@test "re-raising with the EXIT trap still set runs cleanup twice in bash, ksh93, and mksh" {
  observe -k TERM -r "$W/ready" -- "$PROBE" no-disarm "$W/ready"
  case $(lab_shell_family) in
    bash)
      expect_count '^cleanup$' 2
      expect_last "signaled TERM"
      ;;
    ksh93)
      expect_count '^cleanup$' 2
      case $(lab_shell_version) in
        # ksh93u+m 1.0.10 runs the EXIT trap from the re-raise and then exits 1 (measured).
        *93u+m*) expect_last "exited 1" ;;
        *) expect_last "signaled TERM" ;;
      esac
      ;;
    mksh)
      # mksh runs the EXIT trap and exits with the status of its last command.
      expect_count '^cleanup$' 2
      expect_last "exited 0"
      ;;
    dash | zsh | zsh-sh | yash | posh | busybox)
      expect_count '^cleanup$' 1
      expect_last "signaled TERM"
      ;;
    *) false ;;
  esac
}

@test "a second SIGTERM during a disarmed cleanup is discarded and cleanup finishes" {
  observe -k TERM -r "$W/ready" -- "$PROBE" double "$W/ready"
  expect_count '^cleanup-start$' 1
  expect_count '^cleanup-end$' 1
  case $(lab_shell_family) in
    bash | dash | ksh93 | yash | posh | busybox) expect_last "signaled TERM" ;;
    zsh | zsh-sh | mksh) expect_last "exited 143" ;;
    *) false ;;
  esac
}

@test "after a self-kill inside a trap, zsh keeps running the trap and exits by its own exit" {
  observe -- "$PROBE" after-kill
  expect_count '^after-trap$' 0
  case $(lab_shell_family) in
    # bash 3.2 runs one more command before dying and bash 5.3 none; both die by the signal.
    bash) expect_last "signaled TERM" ;;
    dash | yash | posh | busybox)
      expect_count '^after-kill$' 0
      expect_last "signaled TERM"
      ;;
    ksh93)
      expect_count '^after-kill$' 0
      case $(lab_shell_version) in
        # ksh93u+m 1.0.10 stops at the self-kill but reports a plain exit 0 (measured).
        *93u+m*) expect_last "exited 0" ;;
        *) expect_last "signaled TERM" ;;
      esac
      ;;
    zsh | zsh-sh)
      expect_count '^after-kill$' 1
      expect_last "exited 99"
      ;;
    mksh)
      expect_count '^after-kill$' 0
      expect_last "exited 143"
      ;;
    *) false ;;
  esac
}

@test "a self-kill as the last command of a trap action kills zsh once the trap returns" {
  observe -- "$PROBE" after-return
  expect_count '^after-trap$' 0
  case $(lab_shell_family) in
    bash | dash | zsh | zsh-sh | yash | posh | busybox) expect_last "signaled TERM" ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) expect_last "exited 0" ;; # measured in ksh93u+m 1.0.10
        *) expect_last "signaled TERM" ;;
      esac
      ;;
    mksh) expect_last "exited 143" ;;
    *) false ;;
  esac
}

@test "exit \"\$?\" after a child died of SIGTERM: only 256+N and 384+N shells can re-raise it" {
  observe -- "$PROBE" child-status
  case $(lab_shell_family) in
    bash | dash | zsh | zsh-sh | mksh | posh | busybox)
      expect_count '^rc=143$' 1
      expect_last "exited 143"
      ;;
    ksh93)
      expect_count '^rc=271$' 1
      case $(lab_shell_version) in
        # ksh93u+m masks the exit operand to 8 bits (b_return in bltins/cflow.c).
        *93u+m*) expect_last "exited 15" ;;
        # ksh93u+ 2012 turns exit 271 into a death by SIGTERM.
        *) expect_last "signaled TERM" ;;
      esac
      ;;
    yash)
      # yash 2.59+ re-raises any exit status above 384 (maybe_raise in yash.c);
      # 2.58.1 and older exit with the status masked to 8 bits (399 & 255 = 143).
      expect_count '^rc=399$' 1
      case $(lab_shell_version) in
        2.[0-4][0-9]* | 2.5[0-8]* | 2.[0-9].*) expect_last "exited 143" ;;
        *) expect_last "signaled TERM" ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "an outer loop keeps going after a child exits 130 on SIGINT only in wait-and-cooperative-exit shells" {
  lab_shell_words
  observe -g -G -k INT -r "$W/ready" -- "$LOOP" 2 "${LAB_SHELL_WORDS[@]}" "$PROBE" int-child exit "$W/ready"
  case $(lab_shell_family) in
    bash | ksh93)
      expect_count '^iteration 2$' 1
      expect_count '^loop-finished$' 1
      expect_last "exited 0"
      ;;
    dash | zsh | zsh-sh | mksh | yash | posh | busybox)
      expect_count '^iteration 2$' 0
      expect_last "signaled INT"
      ;;
    *) false ;;
  esac
}

@test "an outer loop stops when the child re-raises SIGINT, in every shell" {
  lab_shell_words
  observe -g -G -k INT -r "$W/ready" -- "$LOOP" 2 "${LAB_SHELL_WORDS[@]}" "$PROBE" int-child reraise "$W/ready"
  expect_count '^iteration 1$' 1
  expect_count '^iteration 2$' 0
  expect_last "signaled INT"
}
