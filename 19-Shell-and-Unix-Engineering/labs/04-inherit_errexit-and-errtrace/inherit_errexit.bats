#!/usr/bin/env bats
# inherit_errexit: whether set -e survives into a command substitution, and what it can and cannot stop.
# bash clears -e in the substitution child unless inherit_errexit or POSIX mode is on
# (bash-5.3 subst.c, command_substitute); POSIX.1-2024 XCU 2.15 set -e applies -e to each subshell.

setup() {
  load ../lib/lab
}

# bash_has_inherit_errexit: succeed when the shell under test is bash 4.4 or later.
bash_has_inherit_errexit() {
  run_lab_c 'shopt -s inherit_errexit'
  [ "$status" -eq 0 ]
}

# bash_before_44: succeed when the bash under test reports a version older than 4.4.
bash_before_44() {
  case $(lab_shell_version) in
    [123].* | 4.[0-3].*) return 0 ;;
    *) return 1 ;;
  esac
}

# bash_starts_in_posix: succeed when the bash under test starts in POSIX mode (macOS /bin/sh).
bash_starts_in_posix() {
  run_lab_c '[ -o posix ]'
  [ "$status" -eq 0 ]
}

# assert_no_shopt: the shell under test has no shopt builtin, so no inherit_errexit to toggle.
assert_no_shopt() {
  run_lab_c 'if command -v shopt >/dev/null 2>&1; then echo has-shopt; else echo no-shopt; fi'
  [ "$status" -eq 0 ]
  [ "$output" = no-shopt ]
}

# shellcheck disable=SC2016 # every expansion below belongs to the shell under test
@test "\$- inside a substitution: bash and BusyBox ash drop e unless inherit_errexit or POSIX mode" {
  code='f() { case $- in *e*) echo on ;; *) echo off ;; esac; }; set -e; echo "$(f)"'
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  first=$output
  case $(lab_shell_family) in
    bash)
      if bash_starts_in_posix; then
        [ "$first" = on ]
      else
        [ "$first" = off ]
      fi
      run_lab_c "set -o posix; $code"
      [ "$output" = on ]
      if bash_has_inherit_errexit; then
        run_lab_c "shopt -s inherit_errexit; $code"
        [ "$output" = on ]
      else
        bash_before_44
      fi
      ;;
    busybox) [ "$first" = off ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh) [ "$first" = on ] ;;
    *) false ;;
  esac
}

# shellcheck disable=SC2016 # every expansion below belongs to the shell under test
@test "inherit_errexit stops the substitution at false, and the assignment's status ends the shell" {
  code='set -e; x=$(false; echo one); printf "[%s]\n" "$x"'
  run_lab_c "$code"
  first_status=$status
  first=$output
  case $(lab_shell_family) in
    bash)
      if bash_starts_in_posix; then
        [ "$first_status" -eq 1 ]
        [ -z "$first" ]
      else
        [ "$first_status" -eq 0 ]
        [ "$first" = "[one]" ]
      fi
      if bash_has_inherit_errexit; then
        run_lab_c "shopt -s inherit_errexit; $code"
        [ "$status" -eq 1 ]
        [ -z "$output" ]
      else
        bash_before_44
        run_lab_c 'shopt -s inherit_errexit 2>&1'
        [ "$status" -eq 1 ]
        [[ "$output" == *"inherit_errexit: invalid shell option name"* ]]
      fi
      ;;
    busybox)
      [ "$first_status" -eq 0 ]
      [ "$first" = "[one]" ]
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh)
      [ "$first_status" -eq 1 ]
      [ -z "$first" ]
      ;;
    *) false ;;
  esac
}

# shellcheck disable=SC2016 # every expansion below belongs to the shell under test
@test "inherit_errexit cannot end the parent when the substitution's status is discarded by echo" {
  code='set -e; echo "a $(false; echo one) b"; echo reached'
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  first=$output
  case $(lab_shell_family) in
    bash)
      if bash_starts_in_posix; then
        [ "$first" = "$(printf 'a  b\nreached')" ]
      else
        [ "$first" = "$(printf 'a one b\nreached')" ]
      fi
      if bash_has_inherit_errexit; then
        run_lab_c "shopt -s inherit_errexit; $code"
        [ "$status" -eq 0 ]
        [ "$output" = "$(printf 'a  b\nreached')" ]
      else
        bash_before_44
      fi
      ;;
    busybox) [ "$first" = "$(printf 'a one b\nreached')" ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh) [ "$first" = "$(printf 'a  b\nreached')" ] ;;
    *) false ;;
  esac
}

# shellcheck disable=SC2016 # every expansion below belongs to the shell under test
@test "export x=\$(...) and local x=\$(...) mask the substitution's status even under inherit_errexit" {
  code='set -e; export x=$(false; echo one); echo "x=[$x] reached"'
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  first=$output
  case $(lab_shell_family) in
    bash)
      if bash_starts_in_posix; then
        [ "$first" = "x=[] reached" ]
      else
        [ "$first" = "x=[one] reached" ]
      fi
      if bash_has_inherit_errexit; then
        run_lab_c "shopt -s inherit_errexit; $code"
        [ "$status" -eq 0 ]
        [ "$output" = "x=[] reached" ]
        # the same hole with local, and the fix: declare first, assign on its own line
        run_lab_c 'shopt -s inherit_errexit; set -e
          f() { local x=$(false; echo one); echo "local x=[$x] reached"; }; f'
        [ "$status" -eq 0 ]
        [ "$output" = "local x=[] reached" ]
        run_lab_c 'shopt -s inherit_errexit; set -e
          f() { local x; x=$(false; echo one); echo "local x=[$x] reached"; }; f; echo after'
        [ "$status" -eq 1 ]
        [ -z "$output" ]
      else
        bash_before_44
      fi
      ;;
    busybox) [ "$first" = "x=[one] reached" ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh) [ "$first" = "x=[] reached" ] ;;
    *) false ;;
  esac
}

# shellcheck disable=SC2016 # every expansion below belongs to the shell under test
@test "a substitution inside an if condition: -e stays ignored in bash even with inherit_errexit; dash, mksh and posh apply it" {
  code='set -e; if x=$(false; echo one); then echo "then [$x]"; else echo "else [$x]"; fi'
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  first=$output
  case $(lab_shell_family) in
    bash)
      if bash_starts_in_posix && bash_before_44; then
        # bash 3.2 in POSIX mode (macOS /bin/sh) applies -e in the child, like dash
        [ "$first" = "else []" ]
      else
        [ "$first" = "then [one]" ]
      fi
      if bash_has_inherit_errexit; then
        run_lab_c "shopt -s inherit_errexit; $code"
        [ "$status" -eq 0 ]
        [ "$output" = "then [one]" ]
      else
        bash_before_44
      fi
      ;;
    # the child inherits the parent's "errexit is being ignored" state
    busybox | zsh | zsh-sh | ksh93 | yash) [ "$first" = "then [one]" ] ;;
    # the child starts a fresh errexit context (dash evalbackcmd; mksh and posh execute() without XERROK)
    dash | mksh | posh) [ "$first" = "else []" ] ;;
    *) false ;;
  esac
}

# shellcheck disable=SC2016 # every expansion below belongs to the shell under test
@test "bash process substitution keeps -e without inherit_errexit" {
  case $(lab_shell_family) in
    bash)
      code='f() { case $- in *e*) echo on ;; *) echo off ;; esac; }; set -e; cat <(f); cat <(false; echo one); echo end'
      if bash_starts_in_posix && bash_before_44; then
        # process substitution is unavailable in POSIX mode before bash 5.1 (NEWS, bash-5.1 u.)
        run_lab_c "$code"
        [ "$status" -eq 2 ]
        [[ "$output" == *"syntax error near unexpected token"* ]]
        return 0
      fi
      run_lab_c "$code"
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'on\nend')" ]
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) assert_no_shopt ;;
    *) false ;;
  esac
}

# shellcheck disable=SC2016 # every expansion below belongs to the shell under test
@test "bash 5.3 \${ cmd; } runs in the current shell: with inherit_errexit a failure there ends the script" {
  case $(lab_shell_family) in
    bash)
      run_lab_c 'eval "y=\${ :; }"'
      if [ "$status" -ne 0 ]; then
        # before 5.3 the construct is a bad substitution
        case $(lab_shell_version) in
          [1234].* | 5.[012].*) return 0 ;;
          *) false ;;
        esac
      fi
      if bash_starts_in_posix; then
        # POSIX mode turns inherit_errexit on
        run_lab_c 'set -e; eval "x=\${ false; echo one; }"; echo "x=[$x]"'
        [ "$status" -eq 1 ]
        [ -z "$output" ]
      else
        run_lab_c 'set -e; eval "x=\${ false; echo one; }"; echo "x=[$x]"'
        [ "$status" -eq 0 ]
        [ "$output" = "x=[one]" ]
      fi
      run_lab_c 'shopt -s inherit_errexit; set -e; eval "x=\${ false; echo one; }"; echo "x=[$x]"'
      [ "$status" -eq 1 ]
      [ -z "$output" ]
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) assert_no_shopt ;;
    *) false ;;
  esac
}

@test "set -o posix turns inherit_errexit on, and set +o posix leaves it on (bash 4.4 to 5.3)" {
  case $(lab_shell_family) in
    bash)
      if ! bash_has_inherit_errexit; then
        bash_before_44
        return 0
      fi
      if bash_starts_in_posix; then expect="0 0 0"; else expect="1 0 0"; fi
      # shellcheck disable=SC2016 # the expansions belong to the shell under test
      run_lab_c 'shopt -q inherit_errexit; a=$?; set -o posix; shopt -q inherit_errexit; b=$?
        set +o posix; shopt -q inherit_errexit; echo "$a $b $?"'
      [ "$status" -eq 0 ]
      [ "$output" = "$expect" ]
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) assert_no_shopt ;;
    *) false ;;
  esac
}
