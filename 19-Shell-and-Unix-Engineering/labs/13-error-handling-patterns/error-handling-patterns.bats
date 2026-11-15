#!/usr/bin/env bats
# Tests for errors-probe.sh: the error handling patterns, measured per shell.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/errors-probe.sh"
}

# xargs_flavor: the implementation behind xargs (the shell under test never changes it).
xargs_flavor() {
  local out
  out="$(xargs --version 2>&1 </dev/null) $(xargs --help 2>&1 </dev/null)" || true
  case $out in
    *GNU*) echo gnu ;;
    *BusyBox*) echo busybox ;;
    *"usage: xargs"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

@test "inside a function, native zsh names the function in \$0; every other shell keeps the script name" {
  run_lab "$PROBE" argzero
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh) [ "$output" = "top=errors-probe.sh in_function=inner" ] ;;
    bash | zsh-sh | dash | busybox | mksh | posh | yash | ksh93)
      [ "$output" = "top=errors-probe.sh in_function=errors-probe.sh" ]
      ;;
    *) false ;;
  esac
}

@test "die inside a command substitution ends only the subshell; the assignment carries its status" {
  run_lab "$PROBE" die_subshell
  [ "$status" -eq 0 ]
  [ "$output" = "after_die=reached st=1 value=[]" ]
}

@test "the else branch still sees the condition's status; if ! and an intervening test lose it" {
  run_lab "$PROBE" capture
  [ "$status" -eq 0 ]
  [ "$output" = "else=7 not=0 or=7 late=0" ]
}

@test "export x=\$(cmd) reports export's status; a bare assignment reports cmd's" {
  run_lab "$PROBE" declare_mask
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | busybox | mksh | posh | yash)
      [ "$output" = "bare=3 export=0 local=0 x=[] y=[]" ]
      ;;
    ksh93)
      # ksh93 has no local builtin; typeset inside a function is its equivalent.
      [ "$output" = "bare=3 export=0 local=unsupported x=[] y=[]" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "descriptor 3 carries the upstream status out of a pipeline that reports only its last stage" {
  run_lab "$PROBE" pipe_status
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "data" ]
  [ "${lines[1]}" = "upstream=3" ]
  [ "${#lines[@]}" -eq 2 ]
}

@test "a failure flag set inside a piped while loop survives only where the last stage runs in the current shell" {
  run_lab "$PROBE" pipe_loop
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh | ksh93)
      [ "$output" = "failed=1" ]
      ;;
    bash | zsh-sh | dash | busybox | mksh | posh | yash)
      [ "$output" = "failed=0" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "bare wait returns 0 although the job failed; wait PID returns each job's status" {
  run_lab "$PROBE" wait_status
  [ "$status" -eq 0 ]
  [ "$output" = "bare=0 p1=4 p2=0" ]
}

@test "\${var:?} aborts the subshell before an empty prefix turns \"\$root/\" into \"/\"" {
  run_lab "$PROBE" param_guard
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "unguarded=[/*]" ]
  [[ "$output" == *"root is empty"* ]]
  [[ "$output" != *unreached* ]]
  st=${lines[${#lines[@]} - 1]#guard_st=}
  case $(lab_shell_family) in
    dash) [ "$st" -eq 2 ] ;;
    bash | zsh | zsh-sh | ksh93) [ "$st" -eq 1 ] ;;
    # Not measured locally: POSIX.1-2024 XCU 2.6.2 requires only a non-zero status.
    busybox | mksh | posh | yash) [ "$st" -gt 0 ] ;;
    *) false ;;
  esac
}

@test "printf to a closed stdout fails, except in zsh, which reports success" {
  run_lab "$PROBE" write_error
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh | zsh-sh) [ "$output" = "closed_st=0" ] ;;
    bash | dash | ksh93) [ "$output" = "closed_st=1" ] ;;
    # Not measured locally: POSIX printf requires >0 on error.
    busybox | mksh | posh | yash) [[ "$output" =~ ^closed_st=[1-9][0-9]*$ ]] ;;
    *) false ;;
  esac
}

@test "grep's three outcomes stay distinct when the status is classified with case" {
  run_lab "$PROBE" tristate
  [ "$status" -eq 0 ]
  [[ "$output" =~ ^"match nomatch error("([2-9]|[1-9][0-9]+)")"$ ]]
}

@test "accumulating failures visits every item and exits non-zero at the end" {
  run_lab "$PROBE" accumulate
  [ "$status" -eq 1 ]
  [ "$output" = "failures=2" ]
}

@test "find -exec ... ; hides a failing command; -exec ... + makes find fail" {
  dir=$(mktemp -d "$BATS_TEST_TMPDIR/find.XXXXXX")
  run_lab "$PROBE" find_exec "$dir"
  [ "$status" -eq 0 ]
  [[ "$output" =~ ^semicolon=0\ plus=[1-9][0-9]*$ ]]
}

@test "xargs reports a failing invocation: 123 in GNU and BusyBox, 1 in BSD" {
  run_lab "$PROBE" xargs_status
  [ "$status" -eq 0 ]
  case $(xargs_flavor) in
    gnu | busybox) [ "$output" = "xargs=123" ] ;;
    bsd) [ "$output" = "xargs=1" ] ;;
    *) false ;;
  esac
}

@test "an unknown mode dies with a diagnostic on stderr and status 1" {
  run_lab "$PROBE" no-such-mode
  [ "$status" -eq 1 ]
  [ "$output" = "errors-probe.sh: unknown mode: no-such-mode" ]
}
