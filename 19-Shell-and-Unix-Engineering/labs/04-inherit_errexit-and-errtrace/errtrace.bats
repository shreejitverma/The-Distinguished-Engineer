#!/usr/bin/env bats
# errtrace (set -E): whether the ERR trap reaches shell functions, subshells and command substitutions.
# bash resets the ERR trap on function entry (bash-5.3 execute_cmd.c, execute_function) and in every
# subshell (trap.c, reset_or_restore_signal_handlers) unless error_trace_mode is set.
# POSIX.1-2024 has no ERR condition; the shells that accept one scope it differently.

setup() {
  load ../lib/lab
  # Set the ERR trap only where the shell accepts the condition; dash, posh and yash reject it.
  ERR_TRAP='(trap : ERR) 2>/dev/null && trap "echo trap >&2" ERR'
}

# bash_before_44: succeed when the bash under test reports a version older than 4.4.
bash_before_44() {
  case $(lab_shell_version) in
    [123].* | 4.[0-3].*) return 0 ;;
    *) return 1 ;;
  esac
}

@test "set -o errtrace exists only in bash and BusyBox ash" {
  run_lab_c 'if (set -o errtrace) 2>/dev/null; then echo yes; else echo no; fi'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | busybox) [ "$output" = yes ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh) [ "$output" = no ] ;;
    *) false ;;
  esac
}

@test "set -E is errtrace in bash and BusyBox, silently PUSHD_SILENT in native zsh and emacs in dash" {
  run_lab_c 'if (set -E) 2>/dev/null; then echo accepted; else echo rejected; fi'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | busybox) [ "$output" = accepted ] ;;
    zsh)
      [ "$output" = accepted ]
      run_lab_c 'set -E; if [[ -o pushdsilent ]]; then echo pushdsilent; fi'
      [ "$output" = pushdsilent ]
      ;;
    dash)
      # dash optletters[]: -E is the emacs editing mode, -V is vi
      [ "$output" = accepted ]
      run_lab_c 'set -E; set -o | grep "^emacs"'
      [ "$output" = "emacs           on" ]
      ;;
    zsh-sh | ksh93 | mksh | yash | posh) [ "$output" = rejected ] ;;
    *) false ;;
  esac
}

@test "a POSIX-style function sees the caller's ERR trap: bash and BusyBox only under errtrace" {
  code="$ERR_TRAP; f() { false; echo in-f; }; f"
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | busybox)
      [ "$output" = in-f ]
      run_lab_c "set -E; $code"
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'trap\nin-f')" ]
      ;;
    # traps are global: one trap table shared by every POSIX-style function
    zsh | zsh-sh | ksh93 | mksh) [ "$output" = "$(printf 'trap\nin-f')" ] ;;
    dash | posh | yash) [ "$output" = in-f ] ;;
    *) false ;;
  esac
}

@test "a function whose last command fails: errtrace makes bash run the ERR trap twice, zsh and mksh run it once" {
  code="$ERR_TRAP; f() { false; }; f; echo end"
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | busybox)
      # without errtrace the trap fires once, for the call that returned 1
      [ "$output" = "$(printf 'trap\nend')" ]
      run_lab_c "set -E; $code"
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'trap\ntrap\nend')" ]
      ;;
    # mksh R59 (2020/10/31 and 2025/04/26) also fires once here, but twice for f() { false; return 1; }
    zsh | zsh-sh | mksh) [ "$output" = "$(printf 'trap\nend')" ] ;;
    ksh93) [ "$output" = "$(printf 'trap\ntrap\nend')" ] ;;
    dash | posh | yash) [ "$output" = end ] ;;
    *) false ;;
  esac
}

@test "subshells and command substitutions: bash passes the ERR trap on only under errtrace, BusyBox never" {
  code="$ERR_TRAP; ( false; : ); x=\$(false; :); echo end"
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "$output" = end ]
      run_lab_c "set -E; $code"
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'trap\ntrap\nend')" ]
      ;;
    busybox)
      # forkchild() calls clear_traps(), which clears the ERR slot too, whatever -E says
      [ "$output" = end ]
      run_lab_c "set -E; $code"
      [ "$status" -eq 0 ]
      [ "$output" = end ]
      ;;
    zsh | zsh-sh) [ "$output" = "$(printf 'trap\ntrap\nend')" ] ;;
    # mksh exchild() calls cleartraps(), which includes the ERR slot
    ksh93 | mksh) [ "$output" = end ] ;;
    dash | posh | yash) [ "$output" = end ] ;;
    *) false ;;
  esac
}

# shellcheck disable=SC2016 # every expansion below belongs to the shell under test
@test "under errtrace the trap's stdout inside \$(...) becomes part of the captured value" {
  code='(trap : ERR) 2>/dev/null && trap "echo TRAPOUT" ERR; x=$(false); echo "x=[$x]"'
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      # the parent's trap fires once, for the assignment whose status is 1
      [ "$output" = "$(printf 'TRAPOUT\nx=[]')" ]
      run_lab_c "set -E; $code"
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'TRAPOUT\nx=[TRAPOUT]')" ]
      ;;
    busybox | ksh93 | mksh) [ "$output" = "$(printf 'TRAPOUT\nx=[]')" ] ;;
    zsh | zsh-sh) [ "$output" = "$(printf 'TRAPOUT\nx=[TRAPOUT]')" ] ;;
    dash | posh | yash) [ "$output" = "x=[]" ] ;;
    *) false ;;
  esac
}

@test "an ERR trap set inside a function outlives it in bash 4.4+; bash 3.2 to 4.3 restore the caller's" {
  run_lab_c '(trap : ERR) 2>/dev/null || { echo none; exit 0; }
    trap "echo outer" ERR; f() { trap "echo inner" ERR; }; f; false; echo end'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      if bash_before_44; then
        [ "$output" = "$(printf 'outer\nend')" ]
      else
        [ "$output" = "$(printf 'inner\nend')" ]
      fi
      ;;
    busybox | zsh | zsh-sh | ksh93 | mksh) [ "$output" = "$(printf 'inner\nend')" ] ;;
    dash | posh | yash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

# shellcheck disable=SC2016 # every expansion below belongs to the shell under test
@test "set -e with a shared ERR trap: the trap runs once inside the function, then the shell exits 1" {
  code='(trap : ERR) 2>/dev/null && trap "echo trap \$?" ERR; f() { false; echo no; }; f; echo no'
  case $(lab_shell_family) in
    bash | busybox)
      run_lab_c "set -eE; $code"
      [ "$status" -eq 1 ]
      [ "$output" = "trap 1" ]
      # without -E the trap is reset inside f, and -e exits there, so no trap runs at all
      run_lab_c "set -e; $code"
      [ "$status" -eq 1 ]
      [ -z "$output" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh)
      run_lab_c "set -e; $code"
      [ "$status" -eq 1 ]
      [ "$output" = "trap 1" ]
      ;;
    dash | posh | yash)
      run_lab_c "set -e; $code"
      [ "$status" -eq 1 ]
      [ -z "$output" ]
      ;;
    *) false ;;
  esac
}

@test "shopt -s extdebug turns errtrace on in bash 4.4+, and shopt -u extdebug turns it off again" {
  case $(lab_shell_family) in
    bash)
      # shellcheck disable=SC2016 # the expansions belong to the shell under test
      run_lab_c 'e() { case $- in *E*) printf "%s " E ;; *) printf "%s " - ;; esac; }
        shopt -s extdebug; e; set -E; shopt -u extdebug; e; echo'
      [ "$status" -eq 0 ]
      if bash_before_44; then
        [ "$output" = "- E " ]
      else
        [ "$output" = "E - " ]
      fi
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox)
      run_lab_c 'if command -v shopt >/dev/null 2>&1; then echo has-shopt; else echo no-shopt; fi'
      [ "$output" = no-shopt ]
      ;;
    *) false ;;
  esac
}
