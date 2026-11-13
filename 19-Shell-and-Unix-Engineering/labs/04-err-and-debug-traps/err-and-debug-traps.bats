#!/usr/bin/env bats
# ERR and DEBUG traps: which shells have them, when they fire, what they see, and how bash
# versions differ. probe.sh runs under every shell; bash-probe.sh and err-trace.sh need bash.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  BPROBE="$BATS_TEST_DIRNAME/bash-probe.sh"
  TRACE="$BATS_TEST_DIRNAME/err-trace.sh"
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

@test "ERR and DEBUG are extensions: which shells accept each condition" {
  run_lab "$PROBE" support
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93) [ "$output" = "ERR=yes DEBUG=yes" ] ;;
    mksh | busybox) [ "$output" = "ERR=yes DEBUG=no" ] ;;
    dash | yash | posh) [ "$output" = "ERR=no DEBUG=no" ] ;;
    *) false ;;
  esac
}

@test "ERR sees the failing status in \$? and \$? survives the trap action" {
  run_lab "$PROBE" status
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | busybox) [ "$output" = "err:3
after:3" ] ;;
    dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "ERR obeys the errexit exemptions: if, while, until, ! and non-final && || and pipeline stages" {
  run_lab "$PROBE" exempt
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | busybox) [ "$output" = "fired:last
end" ] ;;
    dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "under set -e the ERR trap runs first and the shell exits with the failure's status" {
  run_lab "$PROBE" errexit
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | busybox)
      [ "$status" -eq 3 ]
      [ "$output" = "err:3" ]
      ;;
    dash | yash | posh)
      [ "$status" -eq 0 ]
      [ "$output" = unsupported ]
      ;;
    *) false ;;
  esac
}

@test "a function whose last command fails: ERR fires once, but twice in ksh93 where name() shares traps" {
  run_lab "$PROBE" funcall
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | mksh | busybox) [ "$output" = "E
end" ] ;;
    ksh93) [ "$output" = "E
E
end" ] ;;
    dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "set -o errtrace makes the same failure fire ERR twice: inside the function and at the call" {
  run_lab "$PROBE" errtrace
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | busybox) [ "$output" = "E
E
end" ] ;;
    zsh | zsh-sh | ksh93 | mksh) [ "$output" = no-errtrace ] ;;
    dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "a pipeline that fails only through pipefail fires ERR, except in ksh93u+ 2012" {
  run_lab "$PROBE" pipefail
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | mksh | busybox) [ "$output" = "E
end" ] ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) [ "$output" = "E
end" ] ;;
        *) [ "$output" = end ] ;;
      esac
      ;;
    dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "a failure inside \$( ) fires ERR only in zsh, which keeps ZERR in subshells" {
  run_lab "$PROBE" cmdsub
  [ "$status" -eq 0 ]
  case $FAMILY in
    zsh | zsh-sh) [ "$output" = "E
x=ok" ] ;;
    bash | ksh93 | mksh | busybox) [ "$output" = "x=ok" ] ;;
    dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "DEBUG runs before each command and sees the previous command's status" {
  run_lab "$PROBE" debug-order
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93) [ "$output" = "D:0
D:1
D:0" ] ;;
    mksh | busybox | dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "bash runs DEBUG once per pipeline stage; zsh and ksh93 once per pipeline" {
  run_lab "$PROBE" debug-pipe
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "runs=4" ] ;;
    zsh | zsh-sh | ksh93) [ "$output" = "runs=2" ] ;;
    mksh | busybox | dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "bash functions do not inherit DEBUG without set -T; zsh and ksh93 name() functions do" {
  run_lab "$PROBE" debug-func
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "runs=2" ] ;;
    zsh | zsh-sh | ksh93) [ "$output" = "runs=4" ] ;;
    mksh | busybox | dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "each DEBUG shell has its own skip-the-next-command protocol" {
  run_lab "$PROBE" debug-skip
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93) [ "$output" = kept ] ;;
    mksh | busybox | dash | yash | posh) [ "$output" = unsupported ] ;;
    *) false ;;
  esac
}

@test "bash: BASH_COMMAND in ERR after a failing function call names the inner command, not the call" {
  run_lab "$BPROBE" stale-command
  case $FAMILY in
    bash)
      [ "$status" -eq 1 ]
      [ "$output" = "cmd=false" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash: with set -E the ERR trap fires in the function and again in the caller" {
  run_lab "$BPROBE" errtrace-twice
  case $FAMILY in
    bash)
      [ "$status" -eq 1 ]
      [ "$output" = "E:g
E:top" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash 4.1 and later: failing [[ ]] and (( )) fire ERR; older bash ignores them" {
  run_lab "$BPROBE" cond-arith
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 4 1; then
        [ "$output" = "E:[[ a == b ]]
E:((0))
end" ]
      else
        [ "$output" = end ]
      fi
      ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash 4.0 and later: a failing ( ) subshell fires ERR in the parent" {
  run_lab "$BPROBE" subshell
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 4 0; then
        [ "$output" = "E:5
end" ]
      else
        [ "$output" = end ]
      fi
      ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash 5.0 and later: command false fires ERR once; older bash fires it twice" {
  run_lab "$BPROBE" command-builtin
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 5 0; then
        [ "$output" = "E
end" ]
      else
        [ "$output" = "E
E
end" ]
      fi
      ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash 4.4 and later: shopt -u extdebug silently turns set -E off" {
  run_lab "$BPROBE" extdebug-errtrace
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 4 4; then
        [ "$output" = errtrace-off ]
      else
        [ "$output" = errtrace-on ]
      fi
      ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash 5.3: BASH_TRAPSIG is NSIG in a DEBUG trap and NSIG + 1 in an ERR trap" {
  run_lab "$BPROBE" trapsig
  case $FAMILY in
    bash)
      [ "$status" -eq 1 ]
      if bash_at_least 5 3; then
        # NSIG is 32 on macOS and 65 on Linux, so assert the relation, not a constant.
        [ "${#lines[@]}" -eq 2 ]
        [[ ${lines[0]} =~ ^debug=([0-9]+)$ ]]
        debug=${BASH_REMATCH[1]}
        [[ ${lines[1]} =~ ^err=([0-9]+)$ ]]
        err=${BASH_REMATCH[1]}
        [ "$debug" -ge 32 ]
        [ "$err" -eq $((debug + 1)) ]
      else
        [ "$output" = "debug=unset
err=unset" ]
      fi
      ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash: a DEBUG trap set inside a function stays set after the function returns" {
  run_lab "$BPROBE" trap-persists
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "D:echo after-setter
after-setter
D:trap - DEBUG" ] ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash: an ERR trap that writes to stdout under set -E lands inside a captured \$( ) value" {
  run_lab "$BPROBE" cmdsub-stdout
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "[TRAPPED
data]" ] ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash: set -T adds a DEBUG run at function entry and one per command in the body" {
  run_lab "$BPROBE" functrace
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "runs=5" ] ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash: BASH_COMMAND in a DEBUG trap is the command text before expansion" {
  run_lab "$BPROBE" unexpanded
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # the expected text is the unexpanded command
  case $FAMILY in
    bash) [ "$output" = 'D:echo "hello $v"
hello world
D:trap - DEBUG' ] ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash: a RETURN trap set in a function persists, skips untraced functions, and fires for ." {
  run_lab "$BPROBE" return-trap
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "R:f
R:top" ] ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "worked example: errexit plus errtrace plus a stack-trace ERR handler" {
  run_lab "$TRACE" 3
  case $FAMILY in
    bash)
      [ "$status" -eq 3 ]
      # The trap $LINENO fix is bash-4.1-alpha CHANGES item p: 4.3.0 reports 18 (measured),
      # 4.0.0 reports 1 (measured), and bash 3.2 reports the function's first line, 17.
      if bash_at_least 4 1; then
        fetch_line=18
      elif bash_at_least 4 0; then
        fetch_line=1
      else
        fetch_line=17
      fi
      [ "$output" = "error: status 3 from [/bin/sh -c \"exit \$1\"]
  at fetch (err-trace.sh:$fetch_line)
  at process (err-trace.sh:23)
  at run (err-trace.sh:27)
  at main (err-trace.sh:30)" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh)
      [ "$status" -eq 2 ]
      [ "$output" = "err-trace.sh: needs bash" ]
      ;;
    *) false ;;
  esac
}

@test "worked example: the script exits with whatever status the failing command returned" {
  run_lab "$TRACE" 7
  case $FAMILY in
    bash)
      [ "$status" -eq 7 ]
      [ "${lines[0]}" = "error: status 7 from [/bin/sh -c \"exit \$1\"]" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh | busybox | dash | yash | posh) [ "$status" -eq 2 ] ;;
    *) false ;;
  esac
}
