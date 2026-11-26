#!/usr/bin/env bats
# Bash introspection variables: the call-stack arrays, LINENO, BASHPID and BASH_SUBSHELL, the
# clock and random variables, SHLVL, FUNCNEST, BASH_COMMAND, BASH_TRAPSIG, and how bash versions
# and other shells differ. probe.sh and lineno.sh run under every shell; bash-probe.sh needs bash.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  LINENO_SH="$BATS_TEST_DIRNAME/lineno.sh"
  BPROBE="$BATS_TEST_DIRNAME/bash-probe.sh"
  FAMILY=$(lab_shell_family)
}

# bash_at_least MAJOR MINOR: succeed if the bash under test is at least MAJOR.MINOR.
bash_at_least() {
  local v major minor
  v=$(lab_shell_version)
  major=${v%%.*}
  minor=${v#*.}
  minor=${minor%%.*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# bash_posix_mode: succeed if the bash under test starts in POSIX mode (macOS /bin/sh does).
# It runs the shell directly, not through run, so it leaves $output and $status alone.
bash_posix_mode() {
  local out
  lab_shell_words
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  out=$("${LAB_SHELL_WORDS[@]}" -c 'case :$SHELLOPTS: in *:posix:*) echo yes ;; *) echo no ;; esac')
  [ "$out" = yes ]
}

# assert_not_bash: every non-bash family must get the guard's answer from bash-probe.sh.
assert_not_bash() {
  case $FAMILY in
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -eq 0 ]
      [ "$output" = not-bash ]
      ;;
    *) false ;;
  esac
}

# set_if MAJOR MINOR: print "set" if the bash under test is at least MAJOR.MINOR, else "unset".
set_if() {
  if bash_at_least "$1" "$2"; then echo set; else echo unset; fi
}

@test "which shells provide FUNCNAME, BASHPID, BASH_SUBSHELL, EPOCHSECONDS, EPOCHREALTIME and SRANDOM" {
  run_lab "$PROBE" support
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      local pid epoch srandom
      pid=$(set_if 4 0)
      epoch=$(set_if 5 0)
      srandom=$(set_if 5 1)
      [ "$output" = "FUNCNAME=show_funcname BASHPID=$pid BASH_SUBSHELL=set EPOCHSECONDS=$epoch EPOCHREALTIME=$epoch SRANDOM=$srandom" ]
      ;;
    busybox)
      local epoch=set patched
      lab_shell_words
      # Alpine's BB_ASH_VERSION patch (aports 0003) misindexes the EPOCH variables, so they expand empty there.
      # shellcheck disable=SC2016 # the expansion belongs to the shell under test
      patched=$("${LAB_SHELL_WORDS[@]}" -c 'echo "${BB_ASH_VERSION-}"')
      if [ -n "$patched" ]; then epoch="unset"; fi
      [ "$output" = "FUNCNAME=show_funcname BASHPID=unset BASH_SUBSHELL=unset EPOCHSECONDS=$epoch EPOCHREALTIME=$epoch SRANDOM=unset" ]
      ;;
    mksh) [ "$output" = "FUNCNAME= BASHPID=set BASH_SUBSHELL=unset EPOCHSECONDS=unset EPOCHREALTIME=set SRANDOM=unset" ] ;;
    zsh | zsh-sh | ksh93 | dash | yash | posh)
      [ "$output" = "FUNCNAME= BASHPID=unset BASH_SUBSHELL=unset EPOCHSECONDS=unset EPOCHREALTIME=unset SRANDOM=unset" ]
      ;;
    *) false ;;
  esac
}

@test "POSIX: \$\$ and PPID keep the parent shell's values inside ( ) and \$( )" {
  run_lab "$PROBE" pid
  [ "$status" -eq 0 ]
  [ "$output" = "paren: dollar=same ppid=same
comsub: dollar=same ppid=same" ]
}

@test "LINENO in a function body: the file line, or a count from the function's first line" {
  run_lab "$LINENO_SH"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | ksh93 | busybox | mksh | posh | yash) [ "$output" = "top=9 func=7" ] ;;
    dash)
      local has_lineno
      lab_shell_words
      # shellcheck disable=SC2016 # the expansion belongs to the shell under test
      has_lineno=$("${LAB_SHELL_WORDS[@]}" -c 'echo "${LINENO-}"')
      if [ -n "$has_lineno" ]; then
        [ "$output" = "top=9 func=3" ]
      else
        # Debian builds dash with --disable-lineno, so LINENO is never set there.
        [ "$output" = "top= func=" ]
      fi
      ;;
    zsh | zsh-sh) [ "$output" = "top=9 func=2" ] ;;
    *) false ;;
  esac
}

@test "bash: FUNCNAME, BASH_SOURCE, BASH_LINENO and caller describe the same stack" {
  run_lab "$BPROBE" stack
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  [ "$output" = "top: FUNCNAME=unset caller=0 NULL
FUNCNAME=h g f main
BASH_SOURCE=stack-lib.bash stack.bash stack.bash stack.bash
BASH_LINENO=7 10 14 0
caller=7 stack.bash
caller 0=7 g stack.bash
caller 1=10 f stack.bash
caller 2=14 main stack.bash
caller 3 fails: status 1
h 4 stack-lib.bash" ]
}

@test "worked example: die prints a stack trace from FUNCNAME, BASH_SOURCE and BASH_LINENO" {
  run_lab "$BPROBE" demo
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 3 ]
  [ "$output" = "demo.bash: fatal: cannot read config: /nonexistent/app.conf
  at read_config (demo.bash:9)
  at load (demo.bash:12)
  at main (demo.bash:14)" ]
}

@test "bash 4.2 and later: FUNCNEST refuses the call past the limit, and the script carries on" {
  run_lab "$BPROBE" funcnest
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  if bash_at_least 4 2; then
    [ "$output" = "funcnest.bash: line 12: f: maximum function nesting level exceeded (3)
after: status=1 depth=3" ]
  else
    [ "$output" = "no limit: reached depth 50
after: status=0 depth=50" ]
  fi
}

@test "bash: BASH_SUBSHELL counts subshell depth and BASHPID (4.0+) tracks the forked process" {
  run_lab "$BPROBE" subshell
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  local yes=yes no=no procsub
  if ! bash_at_least 4 0; then
    yes=nopid
    no=nopid
  fi
  if bash_posix_mode && ! bash_at_least 5 1; then
    procsub="procsub unavailable"
  elif bash_at_least 4 4; then
    procsub="procsub sub=1 fork=$yes"
  else
    procsub="procsub sub=0 fork=$yes"
  fi
  [ "$output" = "top sub=0 fork=$no
paren sub=1 fork=$yes
nested sub=2 fork=$yes
comsub sub=1 fork=$yes
pipe-left sub=1 fork=$yes
pipe-right sub=1 fork=$yes
async sub=1 fork=$yes
$procsub
brace sub=0 fork=$no" ]
}

@test "bash: BASH_ARGV is reversed and gains function frames only under extdebug" {
  run_lab "$BPROBE" argv
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  [ "$output" = "top: c b a|3
f: c b a|3
f: y x c b a|2 3" ]
}

@test "bash 5.0 and later: assigning BASH_ARGV0 sets \$0 until BASH_ARGV0 is unset" {
  run_lab "$BPROBE" argv0
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  if bash_at_least 5 0; then
    [ "$output" = "after assignment: renamed
after unset: renamed" ]
  else
    [ "$output" = "after assignment: orig
after unset: orig" ]
  fi
}

@test "bash: EPOCHREALTIME is seconds.micro (5.0+), EPOCHSECONDS agrees, BASH_MONOSECONDS (5.3+) is whole seconds" {
  run_lab "$BPROBE" epoch
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  if bash_at_least 5 3; then
    [ "$output" = "EPOCHREALTIME seconds.micro
EPOCHSECONDS agrees
BASH_MONOSECONDS integer seconds" ]
  elif bash_at_least 5 0; then
    [ "$output" = "EPOCHREALTIME seconds.micro
EPOCHSECONDS agrees
BASH_MONOSECONDS absent" ]
  else
    [ "$output" = "EPOCHREALTIME absent
EPOCHSECONDS absent
BASH_MONOSECONDS absent" ]
  fi
}

@test "bash 5.1 and later: SRANDOM is a 32-bit value that ignores assignment" {
  run_lab "$BPROBE" srandom
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  if bash_at_least 5 1; then
    [ "$output" = "SRANDOM in range
SRANDOM ignores assignment" ]
  else
    [ "$output" = "SRANDOM absent" ]
  fi
}

@test "bash: a seeded RANDOM repeats, but a command substitution reseeds instead of continuing" {
  run_lab "$BPROBE" random
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  [ "$output" = "seeded sequence repeats
subshell reseeds" ]
}

@test "bash: SHLVL across nested bashes, exec, and the 1000 ceiling, by version" {
  run_lab "$BPROBE" shlvl
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  local last=3 ceiling="not a number"
  if bash_at_least 5 0; then last=2; fi
  if bash_at_least 4 4; then ceiling="reset to 1"; fi
  [ "$output" = "nested-last=$last
nested-not-last=3
exec=2
from-999: $ceiling" ]
}

@test "bash 5.3 and later: BASH_TRAPSIG is the signal number in a trap and 0 in the EXIT trap" {
  run_lab "$BPROBE" trapsig
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  if bash_at_least 5 3; then
    [ "$output" = "USR1 trap: BASH_TRAPSIG is the signal number
EXIT trap: BASH_TRAPSIG=0" ]
  else
    [ "$output" = "USR1 trap: BASH_TRAPSIG unset
EXIT trap: BASH_TRAPSIG=unset" ]
  fi
}

@test "bash 4.3 and later: BASH_COMMAND in a signal trap names the interrupted command" {
  run_lab "$BPROBE" command
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # the expected output contains literal shell text
  local in_trap='in trap: echo "in trap: $BASH_COMMAND"'
  # shellcheck disable=SC2016 # the expected output contains literal shell text
  if bash_at_least 4 3; then in_trap='in trap: kill -USR1 $$'; fi
  # shellcheck disable=SC2016 # the expected output contains literal shell text
  [ "$output" = 'plain: echo "plain: $BASH_COMMAND"'"
$in_trap
EXIT trap: true last" ]
}

@test "bash: BASHPID ignores assignment until unset (4.0+); BASH_SOURCE cannot be unset" {
  run_lab "$BPROBE" unset
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  local before=stored
  if bash_at_least 4 0; then before=ignored; fi
  [ "$output" = "BASHPID=5 before unset: $before; after unset: 5
unset BASH_SOURCE: status 1" ]
}

@test "bash: a function defined by bash -c gets BASH_SOURCE \$0 (5.3), main (4.4 to 5.2), or nothing" {
  run_lab "$BPROBE" cfunc
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  if bash_at_least 5 3; then
    [ "$output" = "BASH_SOURCE=<myname> count=1
caller status 0" ]
  elif bash_at_least 4 4; then
    [ "$output" = "BASH_SOURCE=<main> count=1
caller status 0" ]
  else
    [ "$output" = "BASH_SOURCE=<> count=0
caller status 1" ]
  fi
}

@test "bash 4.4 and later: a function imported from the environment has BASH_SOURCE environment" {
  run_lab "$BPROBE" envfunc
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  if bash_at_least 4 4; then
    [ "$output" = "BASH_SOURCE=<environment> count=1" ]
  else
    [ "$output" = "BASH_SOURCE=<> count=0" ]
  fi
}

@test "bash: the BASH_SOURCE == \$0 guard misreads a script read from stdin; the return test does not" {
  run_lab "$BPROBE" guard
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  [ "$output" = "BASH_SOURCE test: run as a program
BASH_SOURCE test: sourced
BASH_SOURCE test: sourced
return test: run as a program
return test: sourced
return test: run as a program" ]
}

@test "bash 5.3 and later: shopt -s bash_source_fullpath makes BASH_SOURCE absolute" {
  run_lab "$BPROBE" fullpath
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  if bash_at_least 5 3; then
    [ "$output" = "BASH_SOURCE[0] is absolute
BASH_SOURCE[0] is relative: ./srcpath.bash" ]
  else
    [ "$output" = "bash_source_fullpath absent
BASH_SOURCE[0] is relative: ./srcpath.bash" ]
  fi
}

@test "bash: BASH_VERSINFO agrees with BASH_VERSION and is readonly" {
  run_lab "$BPROBE" versinfo
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  [ "$output" = "BASH_VERSINFO matches BASH_VERSION: yes; readonly: yes" ]
}

@test "bash: BASH_EXECUTION_STRING is the -c argument, verbatim" {
  run_lab "$BPROBE" execstring
  if [ "$FAMILY" != bash ]; then
    assert_not_bash
    return
  fi
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # the expected output contains literal shell text
  [ "$output" = 'echo "$BASH_EXECUTION_STRING"' ]
}
