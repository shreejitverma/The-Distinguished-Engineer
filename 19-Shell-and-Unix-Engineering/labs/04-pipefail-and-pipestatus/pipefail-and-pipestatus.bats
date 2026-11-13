#!/usr/bin/env bats
# Tests for pipefail (POSIX.1-2024 XCU 2.9.2 and set) and the per-stage status arrays
# (PIPESTATUS in bash and mksh, pipestatus in native zsh).
# Every expectation is per shell family; an unknown family fails.

setup() {
  load ../lib/lab
  PF="$BATS_TEST_DIRNAME/pipefail-probe.sh"
  PS="$BATS_TEST_DIRNAME/pipestatus-probe.sh"
}

# dash_has_pipefail: succeed if this dash build accepts set -o pipefail when run directly.
# dash 0.5.12 rejects it; 0.5.13 and Debian's patched 0.5.12-7 and later accept it.
dash_has_pipefail() {
  run_lab_c 'set -o pipefail 2>/dev/null && echo yes'
  [ "$output" = yes ]
}

# bash_at_least MAJOR MINOR: succeed if the bash under test is MAJOR.MINOR or later.
bash_at_least() {
  local v major minor
  v=$(lab_shell_version)
  major=${v%%.*}
  minor=${v#*.}
  minor=${minor%%.*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# is_ksh93um: succeed if the ksh93 under test is ksh93u+m, not ksh93u+ 2012.
is_ksh93um() {
  case $(lab_shell_version) in
    *93u+m*) return 0 ;;
    *) return 1 ;;
  esac
}

@test "pipefail support is detected without aborting the shell" {
  run_lab "$PF" support
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | mksh | ksh93) [ "$output" = pipefail=yes ] ;;
    posh) [ "$output" = pipefail=no ] ;;
    dash)
      if dash_has_pipefail; then
        run_lab "$PF" support
        [ "$output" = pipefail=yes ]
      else
        run_lab "$PF" support
        [ "$output" = pipefail=no ]
      fi
      ;;
    *) false ;;
  esac
}

@test "pipefail takes the rightmost failing stage, not the last one to exit" {
  run_lab "$PF" rightmost
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | mksh | ksh93) [ "$output" = status=2 ] ;;
    posh) [ "$output" = unsupported ] ;;
    dash)
      if dash_has_pipefail; then
        run_lab "$PF" rightmost
        [ "$output" = status=2 ]
      else
        run_lab "$PF" rightmost
        [ "$output" = unsupported ]
      fi
      ;;
    *) false ;;
  esac
}

@test "the four rows of the XCU 2.9.2 exit status table" {
  run_lab "$PF" table
  [ "$status" -eq 0 ]
  local want="off=0 off_bang=1 on=3 on_bang=0 on_bang_ok=1"
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | mksh | ksh93) [ "$output" = "$want" ] ;;
    posh) [ "$output" = unsupported ] ;;
    dash)
      if dash_has_pipefail; then
        run_lab "$PF" table
        [ "$output" = "$want" ]
      else
        run_lab "$PF" table
        [ "$output" = unsupported ]
      fi
      ;;
    *) false ;;
  esac
}

@test "set -o pipefail in the last stage: sampled at start, persists where the stage ran" {
  run_lab "$PF" sampled
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    # The last stage runs in the current shell and the status is computed at the end.
    zsh) [ "$output" = "status=1 after=on" ] ;;
    # The last stage runs in the current shell, but the status uses the setting at the start.
    ksh93) [ "$output" = "status=0 after=on" ] ;;
    bash | zsh-sh | yash | busybox | mksh) [ "$output" = "status=0 after=off" ] ;;
    posh) [ "$output" = unsupported ] ;;
    dash)
      if dash_has_pipefail; then
        run_lab "$PF" sampled
        [ "$output" = "status=0 after=off" ]
      else
        run_lab "$PF" sampled
        [ "$output" = unsupported ]
      fi
      ;;
    *) false ;;
  esac
}

@test "set -e exits on a failing upstream stage under pipefail, except in ksh93u+ 2012" {
  run_lab "$PF" errexit
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | mksh)
      [ "$status" -eq 1 ]
      [ "$output" = "" ]
      ;;
    ksh93)
      if is_ksh93um; then
        # Fixed in ksh93u+m on 2020-09-30 (NEWS).
        [ "$status" -eq 1 ]
        [ "$output" = "" ]
      else
        [ "$status" -eq 0 ]
        [ "$output" = survived ]
      fi
      ;;
    posh)
      [ "$status" -eq 0 ]
      [ "$output" = unsupported ]
      ;;
    dash)
      if dash_has_pipefail; then
        run_lab "$PF" errexit
        [ "$status" -eq 1 ]
        [ "$output" = "" ]
      else
        run_lab "$PF" errexit
        [ "$status" -eq 0 ]
        [ "$output" = unsupported ]
      fi
      ;;
    *) false ;;
  esac
}

@test "ksh93 waits for every stage only under pipefail; the others always wait" {
  run_lab "$PF" waitall "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    ksh93) [ "$output" = not-waited ] ;;
    bash | zsh | zsh-sh | yash | busybox | mksh | posh | dash) [ "$output" = waited ] ;;
    *) false ;;
  esac
  run_lab "$PF" waitall "$BATS_TEST_TMPDIR" pipefail
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | mksh | ksh93) [ "$output" = waited ] ;;
    posh) [ "$output" = unsupported ] ;;
    dash)
      if dash_has_pipefail; then
        run_lab "$PF" waitall "$BATS_TEST_TMPDIR" pipefail
        [ "$output" = waited ]
      else
        run_lab "$PF" waitall "$BATS_TEST_TMPDIR" pipefail
        [ "$output" = unsupported ]
      fi
      ;;
    *) false ;;
  esac
}

@test "a background pipeline's pipefail is sampled at start, at wait, or not at all" {
  run_lab "$PF" async
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      if bash_at_least 5 0; then
        # bash 5.0 added J_PIPEFAIL, recorded when the job is created.
        [ "$output" = "start_only=3 wait_only=0" ]
      else
        [ "$output" = "start_only=0 wait_only=3" ]
      fi
      ;;
    ksh93)
      if is_ksh93um; then
        # ksh93u+m, like bash 5, uses the setting in force when the pipeline started.
        [ "$output" = "start_only=3 wait_only=0" ]
      else
        # ksh93u+ 2012 reports the last process's own status for wait PID, ignoring pipefail.
        [ "$output" = "start_only=0 wait_only=0" ]
      fi
      ;;
    # wait PID on a running job reports the last process's own status, ignoring pipefail.
    zsh | zsh-sh) [ "$output" = "start_only=0 wait_only=0" ] ;;
    yash | busybox | mksh) [ "$output" = "start_only=0 wait_only=3" ] ;;
    posh) [ "$output" = unsupported ] ;;
    dash)
      if dash_has_pipefail; then
        run_lab "$PF" async
        [ "$output" = "start_only=0 wait_only=3" ]
      else
        run_lab "$PF" async
        [ "$output" = unsupported ]
      fi
      ;;
    *) false ;;
  esac
}

# pipestatus_case GOT WANT_BASH_MKSH WANT_ZSH: assert the probe output GOT per family.
pipestatus_case() {
  case $(lab_shell_family) in
    bash | mksh) [ "$1" = "$2" ] ;;
    zsh) [ "$1" = "$3" ] ;;
    zsh-sh | yash | busybox | posh | ksh93 | dash) [ "$1" = unsupported ] ;;
    *) false ;;
  esac
}

@test "the status array holds one entry per stage, left to right" {
  run_lab "$PS" basic
  [ "$status" -eq 0 ]
  pipestatus_case "$output" "pipestatus=1 0 3" "pipestatus=1 0 3"
}

@test "the next simple command replaces the array, even a builtin" {
  run_lab "$PS" reset
  [ "$status" -eq 0 ]
  pipestatus_case "$output" "pipestatus=0" "pipestatus=0"
}

@test "an assignment resets PIPESTATUS in bash and mksh but not pipestatus in zsh" {
  run_lab "$PS" piecewise
  [ "$status" -eq 0 ]
  pipestatus_case "$output" "first=1 second=unset" "first=1 second=2"
}

@test "! inverts the status but the array keeps the raw statuses" {
  run_lab "$PS" bang
  [ "$status" -eq 0 ]
  pipestatus_case "$output" "status=1 pipestatus=1 0" "status=1 pipestatus=1 0"
}

@test "eval and a function call leave a one-element array in bash; mksh and zsh keep inner pipelines" {
  run_lab "$PS" collapse
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    # mksh replaces PIPESTATUS only after a command that ran no pipeline of its own.
    mksh) [ "$output" = "function=[1 4] eval=[1 5]" ] ;;
    *) pipestatus_case "$output" "function=[4] eval=[5]" "function=[1 4] eval=[5]" ;;
  esac
}

@test "a writer killed by SIGPIPE shows 141 in its own slot" {
  run_lab "$PS" sigpipe
  [ "$status" -eq 0 ]
  pipestatus_case "$output" "pipestatus=141 0" "pipestatus=141 0"
}

@test "zsh keeps only the first 256 stage statuses; bash and mksh keep all 300" {
  run_lab "$PS" many
  [ "$status" -eq 0 ]
  pipestatus_case "$output" "stages=300 n=300 last=7 rc=7" "stages=300 n=256 last=0 rc=7"
}

@test "an ERR trap fired by pipefail still sees the pipeline's array" {
  run_lab "$PS" trap
  [ "$status" -eq 0 ]
  pipestatus_case "$output" "trap=1 0" "trap=1 0"
}

@test "bash imports PIPESTATUS and SHELLOPTS from the environment, and local - scopes pipefail" {
  run_lab "$PS" bashonly
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      if bash_at_least 4 4; then
        [ "$output" = "imported=[0 rc=1] first=[x] later=[1 2] shellopts=1 local_dash=0" ]
      else
        # local - arrived in bash 4.4; before it the option leaks out of the function.
        [ "$output" = "imported=[0 rc=1] first=[x] later=[1 2] shellopts=1 local_dash=1" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | mksh | posh | ksh93 | dash) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}
