#!/usr/bin/env bats
# Exit code contracts: what a script promises with its status, and the shell
# and utility rules that keep or break that promise between producer and caller.

setup() {
  load ../lib/lab
  HASKEY="$BATS_TEST_DIRNAME/haskey.sh"
  NAIVE="$BATS_TEST_DIRNAME/haskey-naive.sh"
  PASS="$BATS_TEST_DIRNAME/passthrough.sh"
  PROBE="$BATS_TEST_DIRNAME/contract-probe.sh"
  CONF="$BATS_TEST_TMPDIR/app.conf"
  printf 'name=demo\nport=8080\n' >"$CONF"
}

# Compile the C observer; a missing compiler must fail the test, never skip it.
build_observer() {
  OBS="$BATS_TEST_TMPDIR/observe"
  "${CC:-cc}" -Wall -Wextra -Werror -o "$OBS" "$BATS_TEST_DIRNAME/observe.c"
}

# Run the observer from the shell under test; it forks and waits for ARGS.
observe() {
  # shellcheck disable=SC2016 # "$@" is expanded by the shell under test
  run_lab_c '"$@"' sh "$OBS" "$@"
}

# Which xargs implementation is on PATH: gnu, busybox, bsd, or unknown.
xargs_flavor() {
  local out
  out="$(xargs --version 2>&1 </dev/null) $(xargs --help 2>&1 </dev/null)" || true
  case $out in
    *"GNU findutils"*) echo gnu ;;
    *BusyBox*) echo busybox ;;
    *"usage: xargs [-0"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

@test "haskey keeps the four outcomes of its contract distinct" {
  run_lab "$HASKEY" port "$CONF"
  [ "$status" -eq 0 ]
  run_lab "$HASKEY" host "$CONF"
  [ "$status" -eq 1 ]
  run_lab "$HASKEY" 'a.b' "$CONF"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: haskey.sh KEY FILE" ]
  run_lab "$HASKEY" port
  [ "$status" -eq 2 ]
  run_lab "$HASKEY" port "$BATS_TEST_TMPDIR/missing.conf"
  [ "$status" -eq 3 ]
  [[ "$output" == "haskey.sh: cannot read $BATS_TEST_TMPDIR/missing.conf (grep status "* ]]
}

@test "the naive if-grep shape reports an unreadable file as an absent key" {
  run_lab "$NAIVE" port "$CONF"
  [ "$status" -eq 0 ]
  run_lab "$NAIVE" host "$CONF"
  [ "$status" -eq 1 ]
  run_lab "$NAIVE" port "$BATS_TEST_TMPDIR/missing.conf"
  [ "$status" -eq 1 ]
}

@test "grep -q exits 0 on a match even after an unreadable operand" {
  printf 'k\n' >"$BATS_TEST_TMPDIR/hit"
  run_lab "$PROBE" grep-q "$BATS_TEST_TMPDIR/missing" "$BATS_TEST_TMPDIR/hit"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "q=0" ]
  plain=${lines[1]#plain=}
  [ "$plain" -gt 1 ]
}

@test "! maps a failure to 0, so \$? inside then is 0, not the failure" {
  run_lab "$PROBE" if-not
  [ "$status" -eq 0 ]
  [ "$output" = 0 ]
}

@test "else sees the condition's status; an if that runs no branch is 0" {
  run_lab "$PROBE" if-else
  [ "$output" = 3 ]
  run_lab "$PROBE" if-none
  [ "$output" = 0 ]
}

@test "|| rc=\$? under set -e records the exact status and continues" {
  run_lab "$PROBE" capture
  [ "$status" -eq 0 ]
  [ "$output" = "rc=3 continued" ]
}

@test "an EXIT action's last command does not replace the status given to exit" {
  run_lab "$PROBE" trap-keep
  [ "$status" -eq 7 ]
}

@test "a bare exit ending an EXIT action uses \$? from before the action" {
  run_lab "$PROBE" trap-noarg
  case $(lab_shell_family) in
    bash | dash | ksh93 | mksh | yash | busybox) [ "$status" -eq 7 ] ;;
    zsh | zsh-sh | posh) [ "$status" -eq 1 ] ;; # the bare exit takes $? from false
    *) false ;;
  esac
}

@test "a failure count passed to exit wraps modulo 256 unless clamped" {
  run_lab "$PROBE" count-raw 3
  [ "$status" -eq 3 ]
  run_lab "$PROBE" count-raw 256
  [ "$status" -eq 0 ]
  run_lab "$PROBE" count-clamped 256
  [ "$status" -eq 125 ]
  run_lab "$PROBE" count-clamped 3
  [ "$status" -eq 3 ]
}

@test "an integer test on an empty value is an error in some shells and true in others" {
  run_lab "$PROBE" test-empty
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | busybox | yash | posh) [ "$output" = 2 ] ;;
    zsh | zsh-sh | ksh93 | mksh) [ "$output" = 0 ] ;; # the operand is arithmetic: empty is 0, so -eq 0 is true
    *) false ;;
  esac
}

@test "an unbound variable under set -u in sh -c exits 127 in bash, the not-found code" {
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'set -u; echo "$nosuch_var"; echo after'
  [ "$output" != after ]
  case $(lab_shell_family) in
    bash) [ "$status" -eq 127 ] ;; # run_one_command() maps FORCE_EOF to 127
    dash | busybox | yash) [ "$status" -eq 2 ] ;;
    zsh | zsh-sh | ksh93 | mksh | posh) [ "$status" -eq 1 ] ;;
    *) false ;;
  esac
}

@test "a syntax error exits with a code the shell chooses, not the script" {
  run_lab_c 'if'
  case $(lab_shell_family) in
    bash | dash | busybox | yash) [ "$status" -eq 2 ] ;;
    zsh | zsh-sh | mksh | posh) [ "$status" -eq 1 ] ;;
    ksh93) [ "$status" -eq 3 ] ;;
    *) false ;;
  esac
}

@test "exec hands the caller the child's signal death unchanged" {
  build_observer
  lab_shell_words
  # shellcheck disable=SC2016 # $$ belongs to the innermost sh
  observe run "${LAB_SHELL_WORDS[@]}" "$PASS" exec /bin/sh -c 'kill -TERM $$'
  [ "$status" -eq 0 ]
  [ "$output" = "signaled 15" ]
}

@test "exit \"\$rc\" after a signal death launders it into an ordinary exit" {
  build_observer
  lab_shell_words
  # shellcheck disable=SC2016 # $$ belongs to the innermost sh
  observe run "${LAB_SHELL_WORDS[@]}" "$PASS" status /bin/sh -c 'kill -TERM $$'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | zsh | zsh-sh | mksh | posh | busybox) [ "$output" = "exited 143" ] ;;
    yash) [ "$output" = "signaled 15" ] ;; # exit 399 = 384 + 15 makes yash kill itself
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) [ "$output" = "exited 15" ] ;; # exit masks 271 to 271 & 0377
        *) [ "$output" = "signaled 15" ] ;;     # ksh93u+ 2012 re-raises 256 + 15
      esac
      ;;
    *) false ;;
  esac
}

@test "a wrapper whose last command dies reports whatever its shell decides" {
  build_observer
  lab_shell_words
  # shellcheck disable=SC2016 # $$ belongs to the innermost sh
  observe run "${LAB_SHELL_WORDS[@]}" "$PASS" last /bin/sh -c 'kill -TERM $$'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | zsh | zsh-sh | mksh | posh | busybox) [ "$output" = "exited 143" ] ;;
    yash | ksh93) [ "$output" = "signaled 15" ] ;;
    *) false ;;
  esac
}

@test "xargs replaces its children's statuses with its own codes" {
  flavor=$(xargs_flavor)
  run_lab "$PROBE" xargs-nonzero
  case $flavor in
    gnu | busybox) [ "$output" = 123 ] ;;
    bsd) [ "$output" = 1 ] ;;
    *) false ;;
  esac
  run_lab "$PROBE" xargs-255
  [ "${lines[0]}" = "run a" ]
  [ "${#lines[@]}" -eq 2 ]
  case $flavor in
    gnu | busybox) [ "${lines[1]}" = status=124 ] ;;
    bsd) [ "${lines[1]}" = status=1 ] ;;
    *) false ;;
  esac
  run_lab "$PROBE" xargs-signal
  case $flavor in
    gnu | busybox) [ "$output" = 125 ] ;;
    bsd) [ "$output" = 1 ] ;;
    *) false ;;
  esac
}

@test "waitid sees the full exit value on macOS but only the low 8 bits on Linux" {
  build_observer
  observe waitid 300
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Linux) [ "$output" = "wait=44 waitid=44" ] ;;
    Darwin) [ "$output" = "wait=44 waitid=300" ] ;;
    *) false ;;
  esac
}

@test "sysexits.h and stdlib.h give the same values on every platform" {
  build_observer
  observe sysexits
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "EXIT_SUCCESS=0 EXIT_FAILURE=1" ]
  [ "${lines[1]}" = "EX_OK=0 EX__BASE=64 EX__MAX=78" ]
  [ "${lines[2]}" = "EX_USAGE=64 EX_DATAERR=65 EX_NOINPUT=66 EX_NOUSER=67 EX_NOHOST=68" ]
  [ "${lines[3]}" = "EX_UNAVAILABLE=69 EX_SOFTWARE=70 EX_OSERR=71 EX_OSFILE=72" ]
  [ "${lines[4]}" = "EX_CANTCREAT=73 EX_IOERR=74 EX_TEMPFAIL=75 EX_PROTOCOL=76" ]
  [ "${lines[5]}" = "EX_NOPERM=77 EX_CONFIG=78" ]
}

@test "every probe rejects an unknown mode with usage status 2" {
  run_lab "$PROBE" no-such-mode
  [ "$status" -eq 2 ]
  run_lab "$PASS" no-such-mode true
  [ "$status" -eq 2 ]
  build_observer
  observe
  [ "$status" -eq 2 ]
}
