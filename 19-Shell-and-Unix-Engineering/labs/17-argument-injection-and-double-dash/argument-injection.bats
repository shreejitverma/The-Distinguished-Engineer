#!/usr/bin/env bats
# Lab 17: argument injection and the "--" end-of-options delimiter.
# Every test runs code through the shell under test (LAB_SHELL); the C probe
# shows the libc option parser that most external utilities share.

setup() {
  load ../lib/lab
  OPTPARSE="$BATS_TEST_DIRNAME/optparse.sh"
  SWEEP="$BATS_TEST_DIRNAME/sweep.sh"
}

# make_sweep_dir: a directory holding report.txt and an empty file named -oowned.
make_sweep_dir() {
  SWEEP_DIR="$BATS_TEST_TMPDIR/sweep"
  mkdir -p "$SWEEP_DIR"
  printf 'quarterly numbers\n' >"$SWEEP_DIR/report.txt"
  : >"$SWEEP_DIR/-oowned"
}

# build_probe: compile optprobe.c into the test's scratch directory.
build_probe() {
  PROBE="$BATS_TEST_TMPDIR/optprobe"
  "${CC:-cc}" -Wall -Wextra -Werror -o "$PROBE" "$BATS_TEST_DIRNAME/optprobe.c"
}

# probe_libc: the libc the probe was built against, as the probe reports it.
probe_libc() {
  local banner
  banner=$("$PROBE" long) || return 1
  printf '%s\n' "${banner#libc: }"
}

# ---- getopts: what POSIX requires of every shell ----

@test "getopts: options, then operands" {
  run_lab "$OPTPARSE" -a -b val file
  [ "$status" -eq 0 ]
  [ "$output" = "opts: a b=val | operands: <file>" ]
}

@test "getopts: -- ends options, so a later -a is an operand" {
  run_lab "$OPTPARSE" -- -a
  [ "$status" -eq 0 ]
  [ "$output" = "opts: | operands: <-a>" ]
}

@test "getopts: -- in option-argument position is consumed as the argument, not the delimiter" {
  run_lab "$OPTPARSE" -b -- -a
  [ "$status" -eq 0 ]
  [ "$output" = "opts: b=-- a | operands:" ]
}

@test "getopts: the first operand stops parsing, so a later -a stays an operand" {
  run_lab "$OPTPARSE" x -a
  [ "$status" -eq 0 ]
  [ "$output" = "opts: | operands: <x> <-a>" ]
}

@test "getopts: a lone - is an operand and stops parsing" {
  run_lab "$OPTPARSE" - -a
  [ "$status" -eq 0 ]
  [ "$output" = "opts: | operands: <-> <-a>" ]
}

@test "getopts: only the first -- is a delimiter; a second one is an operand" {
  run_lab "$OPTPARSE" -a -- -- -a
  [ "$status" -eq 0 ]
  [ "$output" = "opts: a | operands: <--> <-a>" ]
}

# ---- the attack: a filename that is an option ----

@test "glob: a file named -oowned hijacks -o and the report goes to a new file" {
  make_sweep_dir
  run_lab "$SWEEP" glob "$SWEEP_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "clobbered: owned now holds quarterly numbers" ]
  [ -f "$SWEEP_DIR/owned" ]
}

@test "glob with --: the same file is read as an operand" {
  make_sweep_dir
  run_lab "$SWEEP" dashdash "$SWEEP_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "quarterly numbers" ]
  [ ! -e "$SWEEP_DIR/owned" ]
}

@test "glob with ./ prefix: no word can start with -, with or without --" {
  make_sweep_dir
  run_lab "$SWEEP" dotslash "$SWEEP_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "quarterly numbers" ]
  [ ! -e "$SWEEP_DIR/owned" ]
}

@test "real rm: rm * with a file named -f deletes the other file and keeps -f" {
  mkdir "$BATS_TEST_TMPDIR/rmdir"
  : >"$BATS_TEST_TMPDIR/rmdir/-f"
  : >"$BATS_TEST_TMPDIR/rmdir/keep"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'cd "$1" || exit 1; LC_ALL=C; export LC_ALL; rm *; ls -A' sh "$BATS_TEST_TMPDIR/rmdir"
  [ "$status" -eq 0 ]
  [ "$output" = "-f" ]
}

@test "real rm: rm -- * deletes both files" {
  mkdir "$BATS_TEST_TMPDIR/rmdir"
  : >"$BATS_TEST_TMPDIR/rmdir/-f"
  : >"$BATS_TEST_TMPDIR/rmdir/keep"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'cd "$1" || exit 1; LC_ALL=C; export LC_ALL; rm -- *; ls -A' sh "$BATS_TEST_TMPDIR/rmdir"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
  [ ! -e "$BATS_TEST_TMPDIR/rmdir/-f" ]
}

# ---- shell built-ins ----

@test "set: an unguarded set \"\$x\" with x=-f turns on noglob" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'x=-f; set "$x"; case $- in *f*) echo noglob-on ;; *) echo "positional:$1" ;; esac'
  [ "$status" -eq 0 ]
  [ "$output" = noglob-on ]
}

@test "set: set -- \"\$x\" stores -f as \$1" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'x=-f; set -- "$x"; case $- in *f*) echo noglob-on ;; *) echo "positional:$1" ;; esac'
  [ "$status" -eq 0 ]
  [ "$output" = "positional:-f" ]
}

@test "printf: -- is discarded and a dash-leading data operand is printed" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'v=-n; printf -- "%s\n" "$v"; printf "%s\n" "$v"'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf -- '-n\n-n')" ]
}

@test "echo: -- is printed, never discarded" {
  run_lab_c 'unset ECHO_STYLE; echo -- x'
  [ "$status" -eq 0 ]
  [ "$output" = "-- x" ]
}

@test "echo: a variable holding -n is swallowed as an option in most shells" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'unset ECHO_STYLE; v=-n; echo "$v"; echo end'
  [ "$status" -eq 0 ]
  echo_out=$output
  case $(lab_shell_family) in
    bash)
      # A bash built for UNIX03 echo (macOS /bin/sh) takes no echo options at all,
      # even though shopt reports xpg_echo off there; detect it by its -e handling.
      run_lab_c 'echo -e x'
      if [ "$output" = "-e x" ]; then
        [ "$echo_out" = "$(printf -- '-n\nend')" ]
      else
        [ "$echo_out" = end ]
      fi
      ;;
    yash) [ "$echo_out" = "$(printf -- '-n\nend')" ] ;; # default ECHO_STYLE is SYSV: no options
    dash | zsh | zsh-sh | ksh93 | mksh | posh | busybox) [ "$echo_out" = end ] ;;
    *) false ;;
  esac
}

@test "cd: -- does not protect the operand -, which still means OLDPWD; ./- does" {
  mkdir -p "$BATS_TEST_TMPDIR/work/-"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '
    cd "$1" || exit 1
    cd / || exit 1
    cd -- - >/dev/null || exit 1
    printf "after-dashdash:%s\n" "${PWD##*/}"
    cd "./-" || exit 1
    printf "after-dotslash:%s\n" "${PWD##*/}"
  ' sh "$BATS_TEST_TMPDIR/work"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'after-dashdash:work\nafter-dotslash:-')" ]
}

@test "exec: POSIX.1-2024 requires --, and dash and posh run it as a command name" {
  run_lab_c 'exec -- printf "%s\n" ok'
  case $(lab_shell_family) in
    dash | posh)
      [ "$status" -eq 127 ]
      [ "$output" != ok ]
      ;;
    bash | zsh | zsh-sh | ksh93 | mksh | yash | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = ok ]
      ;;
    *) false ;;
  esac
}

@test "eval: -- is optional in POSIX; dash and BusyBox run it as a command name" {
  run_lab_c 'eval -- "echo ok" 2>/dev/null; echo "rc=$?"'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    dash | busybox) [ "$output" = "rc=127" ] ;;
    bash | zsh | zsh-sh | ksh93 | mksh | yash | posh) [ "$output" = "$(printf 'ok\nrc=0')" ] ;;
    *) false ;;
  esac
}

@test "eval: a leading space is the portable guard for dash-leading code" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'code="-x"; eval " $code" 2>/dev/null; echo "rc=$?"; case $- in *x*) echo xtrace-on ;; *) echo xtrace-off ;; esac'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'rc=127\nxtrace-off')" ]
}

@test "sh -c: a dash-leading command string is parsed as options unless -- precedes it" {
  run_lab -c -x
  without=$status
  run_lab -c -- -x
  with=$status
  [ "$with" -eq 127 ]
  case $(lab_shell_family) in
    bash | dash | ksh93 | yash | busybox) [ "$without" -eq 2 ] ;;
    zsh | zsh-sh | mksh | posh) [ "$without" -eq 1 ] ;;
    *) false ;;
  esac
}

# ---- the C library parser behind most external utilities ----

@test "getopt_long: a unique prefix selects a long option (--checkpoint-a)" {
  build_probe
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'unset POSIXLY_CORRECT; exec "$@"' sh "$PROBE" long --checkpoint-a=exec=sh
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "opt: checkpoint-action=exec=sh" ]
  [ "${#lines[@]}" -eq 2 ]
}

@test "getopt_long: an exact name wins over a longer option it prefixes; an ambiguous prefix is an error" {
  build_probe
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'unset POSIXLY_CORRECT; "$@" --checkpoint; "$@" --check' sh "$PROBE" long
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "opt: checkpoint" ]
  [ "${lines[3]}" = "error" ]
  [ "${#lines[@]}" -eq 4 ]
}

@test "getopt_long: options after an operand are still options (permutation)" {
  build_probe
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'unset POSIXLY_CORRECT; exec "$@"' sh "$PROBE" long report.txt -o out
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "opt: o=out" ]
  [ "${lines[2]}" = "operand: <report.txt>" ]
  [ "${#lines[@]}" -eq 3 ]
}

@test "getopt_long: a leading + in the option string stops at the first operand" {
  build_probe
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'unset POSIXLY_CORRECT; exec "$@"' sh "$PROBE" long-plus report.txt -o out
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "operand: <report.txt>" ]
  [ "${lines[2]}" = "operand: <-o>" ]
  [ "${lines[3]}" = "operand: <out>" ]
}

@test "getopt_long: -- ends options even under permutation" {
  build_probe
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'unset POSIXLY_CORRECT; exec "$@"' sh "$PROBE" long -- -o out
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "operand: <-o>" ]
  [ "${lines[2]}" = "operand: <out>" ]
  [ "${#lines[@]}" -eq 3 ]
}

@test "getopt_long: POSIXLY_CORRECT stops permutation in glibc and Apple libc, not in musl" {
  build_probe
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'POSIXLY_CORRECT=1; export POSIXLY_CORRECT; exec "$@"' sh "$PROBE" long report.txt -o out
  [ "$status" -eq 0 ]
  case $(probe_libc) in
    glibc | apple)
      [ "${lines[1]}" = "operand: <report.txt>" ]
      [ "${lines[2]}" = "operand: <-o>" ]
      ;;
    musl)
      [ "${lines[1]}" = "opt: o=out" ]
      [ "${lines[2]}" = "operand: <report.txt>" ]
      ;;
    *) false ;;
  esac
}

@test "getopt: glibc permutes plain getopt too; musl and Apple libc stop at the first operand" {
  build_probe
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'unset POSIXLY_CORRECT; exec "$@"' sh "$PROBE" short report.txt -o out
  [ "$status" -eq 0 ]
  case $(probe_libc) in
    glibc)
      [ "${lines[1]}" = "opt: o=out" ]
      [ "${lines[2]}" = "operand: <report.txt>" ]
      ;;
    musl | apple)
      [ "${lines[1]}" = "operand: <report.txt>" ]
      [ "${lines[2]}" = "operand: <-o>" ]
      [ "${lines[3]}" = "operand: <out>" ]
      ;;
    *) false ;;
  esac
}
