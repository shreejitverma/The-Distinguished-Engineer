#!/usr/bin/env bats
# Tests for userland probes across GNU, BSD, BusyBox, and uutils.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/probe_userland.sh"
}

@test "detect userland flavor from core utilities" {
  run_lab "$PROBE" flavor
  [ "$status" -eq 0 ]
  if [ "$(uname -s)" = Darwin ]; then
    [ "$output" = "bsd" ]
  else
    case "$output" in
      gnu | busybox | uutils) ;;
      *) false ;;
    esac
  fi
}

@test "argument permutation: GNU, BusyBox and uutils permute, BSD stops at the first operand" {
  run_lab "$PROBE" flavor
  local flavor=$output
  run_lab "$PROBE" permute "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case "$flavor" in
    bsd) [ "$output" = "strict" ] ;;
    gnu | busybox | uutils) [ "$output" = "permute" ] ;;
    *) false ;;
  esac
}

@test "POSIXLY_CORRECT disables permutation where the getopt honors it (glibc, BSD), not BusyBox on musl or uutils" {
  run_lab "$PROBE" flavor
  local flavor=$output musl=no
  # musl getopt_long ignores POSIXLY_CORRECT, so BusyBox on musl (Alpine) keeps permuting.
  for f in /lib/ld-musl-*.so.1; do
    [ -e "$f" ] && musl=yes
  done
  # shellcheck disable=SC2016 # LAB_SHELL expansion belongs to the harness
  run_lab_c 'POSIXLY_CORRECT=1 "$1" permute "$2"' "sh" "$PROBE" "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  # uutils 0.12.0 never reads POSIXLY_CORRECT for option order (measured on Linux and macOS).
  if [ "$flavor" = uutils ] || { [ "$flavor" = busybox ] && [ "$musl" = yes ]; }; then
    [ "$output" = "permute" ]
  else
    [ "$output" = "strict" ]
  fi
}

@test "file size probe normalizes stat differences across implementations" {
  local test_file="$BATS_TEST_TMPDIR/stat_test.txt"
  printf '0123456789abcdef\n' >"$test_file"
  run_lab "$PROBE" stat_size "$test_file"
  [ "$status" -eq 0 ]
  [ "$output" = "17" ]
}

@test "epoch timestamp formatting resolves date option incompatibilities" {
  run_lab "$PROBE" date_epoch 1700000000
  [ "$status" -eq 0 ]
  [ "$output" = "2023-11-14 22:13:20" ]
}

@test "in-place sed probe reconciles backup extension syntax" {
  local test_file="$BATS_TEST_TMPDIR/sed_test.txt"
  printf 'alpha_omega\n' >"$test_file"
  run_lab "$PROBE" sed_inplace "$test_file" "alpha" "beta"
  [ "$status" -eq 0 ]
  run_lab_c 'cat "$1"' "sh" "$test_file"
  [ "$status" -eq 0 ]
  [ "$output" = "beta_omega" ]
}

@test "shell builtin echo differs across families while printf is portable" {
  run_lab_c 'printf "%s" "hello"'
  [ "$status" -eq 0 ]
  [ "$output" = "hello" ]

  local family xsi=no
  family=$(lab_shell_family)
  if [ "$family" = bash ]; then
    # Apple's bash 3.2 as /bin/sh (POSIX mode) and any bash in POSIX mode with
    # xpg_echo follow the XSI rule: a leading -n is printed as text.
    # shellcheck disable=SC2016 # these expansions belong to the shell under test
    run_lab_c '[ -o posix ] || exit 1
      case $BASH_VERSION in
        3.2.*) [ "$(uname -s)" = Darwin ] ;;
        *) shopt -q xpg_echo ;;
      esac'
    [ "$status" -eq 0 ] && xsi=yes
  fi

  run_lab_c 'echo -n "hello"'
  [ "$status" -eq 0 ]
  case $family in
    bash)
      if [ "$xsi" = yes ]; then
        [ "$output" = "-n hello" ]
      else
        [ "$output" = "hello" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | mksh | posh | ksh93 | dash)
      # dash 0.5.12 and posh 0.14.1 accept -n (measured in the shell matrix images).
      [ "$output" = "hello" ]
      ;;
    *)
      false
      ;;
  esac
}
