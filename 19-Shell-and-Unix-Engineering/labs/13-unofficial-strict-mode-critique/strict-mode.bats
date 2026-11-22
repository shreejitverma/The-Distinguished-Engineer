#!/usr/bin/env bats
# Tests for Unofficial Strict Mode Critique: each claim of `set -euo pipefail; IFS=$'\n\t'`
# checked against POSIX.1-2024 XCU 2.2.4, 2.6.5, 2.8.1, 2.9.2, and set, per shell family.
# Code under test runs only through run_lab SCRIPT or run_lab_c CODE (labs/lib/lab.bash).

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/strict-probe.sh"
}

# shell_has_dollar_single: succeed when the shell under test decodes $'\t' to a tab.
shell_has_dollar_single() {
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'printf %s $'"'"'\t'"'"
  [ "$output" = "$(printf '\t')" ]
}

# shell_has_pipefail: succeed when `set -o pipefail` works in the shell under test.
shell_has_pipefail() {
  run_lab_c '(set -o pipefail) 2>/dev/null'
  [ "$status" -eq 0 ]
}

# bash_keeps_errexit_in_cmdsub: succeed when the bash under test is in POSIX mode or has
# inherit_errexit on, the two states in which bash keeps -e inside $(...).
bash_keeps_errexit_in_cmdsub() {
  run_lab_c '[ -o posix ] || shopt -q inherit_errexit 2>/dev/null'
  [ "$status" -eq 0 ]
}

# bash_is_posix_mode: succeed when the bash under test runs in POSIX mode (macOS /bin/sh).
bash_is_posix_mode() {
  run_lab_c '[ -o posix ]'
  [ "$status" -eq 0 ]
}

@test "IFS=\$'\\n\\t' is two bytes where \$'...' exists and five literal bytes where it does not" {
  family=$(lab_shell_family)
  if shell_has_dollar_single; then has=yes; else has=no; fi
  run_lab "$PROBE" ifs_dollar
  [ "$status" -eq 0 ]
  case $family in
    bash | zsh | zsh-sh | ksh93 | mksh | yash | busybox)
      [ "$has" = yes ]
      [ "$output" = "ifs_len=2 fields=1 <internet>" ]
      ;;
    posh)
      [ "$has" = no ]
      # IFS is $ \ n \ t, so "internet" splits at every n and t (XCU 2.6.5).
      [ "$output" = "ifs_len=5 fields=4 <i> <> <er> <e>" ]
      ;;
    dash)
      # dash 0.5.12 lacks $'...'; dash 0.5.13 has it.
      if [ "$has" = yes ]; then
        [ "$output" = "ifs_len=2 fields=1 <internet>" ]
      else
        [ "$output" = "ifs_len=5 fields=4 <i> <> <er> <e>" ]
      fi
      ;;
    *) false ;;
  esac
}

@test "strict IFS keeps spaces inside fields, stops read from splitting on spaces, and joins \"\$*\" with newline" {
  run_lab "$PROBE" ifs_portable
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh)
      # Native zsh never field-splits an unquoted parameter expansion.
      [ "${lines[0]}" = "ifs_len=2 fields=1 <Aaron Maxwell" ]
      [ "${lines[1]}" = "Wayne Gretzky>" ]
      [ "${lines[2]}" = "read_first=<alpha beta> read_rest=<>" ]
      [ "${lines[3]}" = "star_join_hex=610a62" ]
      ;;
    bash | zsh-sh | dash | ksh93 | mksh | yash | busybox | posh)
      [ "${lines[0]}" = "ifs_len=2 fields=2 <Aaron Maxwell> <Wayne Gretzky>" ]
      [ "${lines[1]}" = "read_first=<alpha beta> read_rest=<>" ]
      [ "${lines[2]}" = "star_join_hex=610a62" ]
      ;;
    *) false ;;
  esac
}

@test "strict IFS does not stop pathname expansion, and a tab in a file name still splits it" {
  cd "$BATS_TEST_TMPDIR"
  : >a.txt
  : >b.txt
  : >'a b.log'
  : >"$(printf 'tab\tc.log')"
  run_lab "$PROBE" ifs_glob
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh)
      # Native zsh neither splits nor globs an unquoted parameter expansion.
      [ "${lines[0]}" = "glob fields=1 <*.txt>" ]
      [ "${lines[1]}" = "find fields=1 <./a b.log" ]
      [ "${lines[2]}" = "$(printf './tab\tc.log>')" ]
      ;;
    bash | zsh-sh | dash | ksh93 | mksh | yash | busybox | posh)
      [ "${lines[0]}" = "glob fields=2 <a.txt> <b.txt>" ]
      [ "${lines[1]}" = "find fields=3 <./a b.log> <./tab> <c.log>" ]
      ;;
    *) false ;;
  esac
}

@test "set -u aborts inside an if condition with no set -e: an expansion error is not an errexit failure" {
  run_lab "$PROBE" nounset_cond
  [ "$status" -ne 0 ]
  [[ "$output" == *undef_var* ]]
  [[ "$output" != *branch* ]]
  [[ "$output" != *survived* ]]
}

@test "set -u aborts inside a function called on the left of ||: the handler never runs" {
  run_lab "$PROBE" nounset_or
  [ "$status" -ne 0 ]
  [[ "$output" == *undef_var* ]]
  [[ "$output" != *handled* ]]
  [[ "$output" != *survived* ]]
}

@test "set -u in a command substitution ends only the subshell; the parent continues with a non-zero status" {
  run_lab "$PROBE" nounset_cmdsub
  [ "$status" -eq 0 ]
  last=${lines[${#lines[@]} - 1]}
  [[ "$last" == "after st="[1-9]*" x=<>" ]]
  [[ "$output" != *inner* ]]
}

@test "set -u aborts on an unset \$1 but exempts \"\$@\", except posh and ksh93u+ 2012" {
  run_lab "$PROBE" nounset_positional
  [ "${lines[0]}" = "default=none n=0" ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | mksh | yash | busybox)
      [ "$status" -ne 0 ]
      [ "${lines[1]}" = "at=[]" ]
      [[ "$output" != *one=* ]]
      ;;
    posh)
      # posh 0.14.1 aborts on an empty "$@" under -u, which POSIX exempts.
      [ "$status" -ne 0 ]
      [[ "$output" != *at=* ]]
      ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*)
          # ksh93u+m NEWS 2020-06-08: -u now errors on a nonexistent positional parameter.
          [ "$status" -ne 0 ]
          [ "${lines[1]}" = "at=[]" ]
          [[ "$output" != *one=* ]]
          ;;
        *)
          # ksh93u+ 2012 expands an unset $1 to empty under -u.
          [ "$status" -eq 0 ]
          [ "${lines[2]}" = "one=" ]
          [ "${lines[3]}" = "survived" ]
          ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "set -u never catches an empty variable: \"\$root/*\" becomes /* and only \${root:?} stops it" {
  run_lab "$PROBE" nounset_null
  [ "$status" -ne 0 ]
  [ "${lines[0]}" = "target=/*" ]
  [[ "$output" == *"root is empty"* ]]
  [[ "$output" != *survived* ]]
}

@test "set -u on an empty array: bash before 4.4 aborts, bash 4.4 and later expand nothing" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'set -u; eval "a=()"; eval "printf \"[%s]\" \"\${a[@]}\""; printf "survived\n"'
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        3.* | 4.[0-3].*)
          [ "$status" -ne 0 ]
          [[ "$output" == *"a[@]: unbound variable"* ]]
          [[ "$output" != *survived* ]]
          ;;
        *)
          [ "$status" -eq 0 ]
          [ "$output" = "[]survived" ]
          ;;
      esac
      ;;
    zsh | zsh-sh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = "[]survived" ]
      ;;
    mksh)
      # mksh R59 treats "${a[@]}" of an array with no elements as unset under -u.
      [ "$status" -ne 0 ]
      [[ "$output" == *"a[@]: parameter not set"* ]]
      [[ "$output" != *survived* ]]
      ;;
    ksh93)
      # In ksh93, a=() makes an empty compound variable, and "${a[@]}" prints it as ( and ).
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "[(" ]
      [ "${lines[1]}" = ")]survived" ]
      ;;
    dash | posh | busybox)
      # No arrays: a=() is a syntax error and the shell exits.
      [ "$status" -ne 0 ]
      [[ "$output" != *survived* ]]
      ;;
    *) false ;;
  esac
}

@test "pipefail turns a reader that exits early into a fatal SIGPIPE status under set -e" {
  family=$(lab_shell_family)
  version=$(lab_shell_version)
  if shell_has_pipefail; then has=yes; else has=no; fi
  run_lab "$PROBE" pipefail_sigpipe
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "control=<survived>" ]
  case $family in
    bash | zsh | zsh-sh | mksh | busybox)
      [ "$has" = yes ]
      [ "${lines[1]}" = "pipefail=on status=141 errexit_out=<>" ]
      ;;
    yash)
      # yash reports a signal death as 384 + N.
      [ "$has" = yes ]
      [ "${lines[1]}" = "pipefail=on status=397 errexit_out=<>" ]
      ;;
    ksh93)
      [ "$has" = yes ]
      case $version in
        *93u+m*)
          # ksh93u+m pipes are sockets, so yes may see ECONNRESET instead of SIGPIPE.
          [[ "${lines[1]}" == "pipefail=on status="[1-9]*" errexit_out=<>" ]]
          ;;
        *)
          # ksh93u+ 2012 ignores a pipefail failure under set -e (fixed in the ksh93u+m NEWS entry of 2020-09-30).
          [ "${lines[1]}" = "pipefail=on status=13 errexit_out=<survived>" ]
          ;;
      esac
      ;;
    dash)
      if [ "$has" = yes ]; then
        [ "${lines[1]}" = "pipefail=on status=141 errexit_out=<>" ]
      else
        [ "${lines[1]}" = "pipefail=unsupported" ]
      fi
      ;;
    posh)
      [ "$has" = no ]
      [ "${lines[1]}" = "pipefail=unsupported" ]
      ;;
    *) false ;;
  esac
}

@test "set -o pipefail in a shell that lacks it aborts the script instead of continuing" {
  family=$(lab_shell_family)
  if shell_has_pipefail; then has=yes; else has=no; fi
  run_lab_c 'set -o pipefail; printf "after\n"'
  case $family in
    bash | zsh | zsh-sh | ksh93 | mksh | yash | busybox)
      [ "$has" = yes ]
      [ "$status" -eq 0 ]
      [ "$output" = "after" ]
      ;;
    posh)
      [ "$has" = no ]
      [ "$status" -ne 0 ]
      [[ "$output" != *after* ]]
      ;;
    dash)
      if [ "$has" = yes ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "after" ]
      else
        [ "$status" -ne 0 ]
        [[ "$output" != *after* ]]
      fi
      ;;
    *) false ;;
  esac
}

@test "a counter at zero ends a set -e script through expr; an \$(( )) assignment does not" {
  run_lab "$PROBE" expr_zero
  [ "$status" -eq 0 ]
  [ "$output" = "expr_st=1 expr_out=<> arith_out=<survived i=1>" ]
}

@test "((i++)) from zero is fatal under set -e in bash 4.1+ and zsh, not in bash 3.2 or ksh93" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'set -e; i=0; ((i++)); printf "survived i=%s\n" "$i"'
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        3.* | 4.0.*)
          # bash-4.1-alpha CHANGES item j: [[ and (( became subject to set -e.
          [ "$status" -eq 0 ]
          [ "$output" = "survived i=1" ]
          ;;
        *)
          [ "$status" -eq 1 ]
          [ "$output" = "" ]
          ;;
      esac
      ;;
    zsh | zsh-sh | mksh)
      [ "$status" -eq 1 ]
      [ "$output" = "" ]
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = "survived i=1" ]
      ;;
    dash | posh | yash | busybox)
      # No arithmetic command: ((i++)) parses as nested subshells running "i++".
      [ "$status" -eq 127 ]
      [[ "$output" != *survived* ]]
      ;;
    *) false ;;
  esac
}

@test "grep -c with no match is fatal under set -e, and || true also hides a real grep error" {
  cd "$BATS_TEST_TMPDIR"
  run_lab "$PROBE" grep_count
  [ "$status" -eq 0 ]
  [ "$output" = "plain_st=1 plain=<> ortrue=<count=0> masked=<count=>" ]
}

@test "COND && CMD as the last command makes the script exit 1 although nothing failed" {
  cd "$BATS_TEST_TMPDIR"
  run_lab "$PROBE" andor_last
  [ "$status" -eq 1 ]
  [ "$output" = "" ]
  run_lab "$PROBE" andor_notlast
  [ "$status" -eq 0 ]
  [ "$output" = "done" ]
}

@test "a failure in a non-final && operand is ignored, but errexit applies inside a final { } group" {
  run_lab "$PROBE" brace_group
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | ksh93 | mksh | yash | busybox)
      [ "$output" = "chain_st=0 chain=<next> group_st=1 group=<>" ]
      ;;
    posh)
      # posh 0.14.1 passes XERROK to every operand of an AND-OR list.
      [ "$output" = "chain_st=0 chain=<next> group_st=0 group=<innerafter>" ]
      ;;
    *) false ;;
  esac
}

@test "strict mode without inherit_errexit: native bash and BusyBox ash run past a failure inside \$(...)" {
  family=$(lab_shell_family)
  keeps=no
  if [ "$family" = bash ] && bash_keeps_errexit_in_cmdsub; then keeps=yes; fi
  run_lab "$PROBE" cmdsub_leak
  case $family in
    bash)
      if [ "$keeps" = yes ]; then
        [ "$status" -eq 1 ]
        [ "$output" = "" ]
      else
        [ "$status" -eq 0 ]
        [ "$output" = "v=<leaked>" ]
      fi
      ;;
    busybox)
      [ "$status" -eq 0 ]
      [ "$output" = "v=<leaked>" ]
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh)
      [ "$status" -eq 1 ]
      [ "$output" = "" ]
      ;;
    *) false ;;
  esac
}

@test "shopt -s inherit_errexit closes the \$(...) leak in bash 4.4 and later only" {
  family=$(lab_shell_family)
  posix=no
  if [ "$family" = bash ] && bash_is_posix_mode; then posix=yes; fi
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'shopt -s inherit_errexit 2>/dev/null; set -e; v=$(false; printf leaked); printf "v=<%s>\n" "$v"'
  case $family in
    bash)
      case $posix:$(lab_shell_version) in
        no:3.* | no:4.[0-3].*)
          # No such shopt before bash-4.4-rc1, so the leak stays.
          [ "$status" -eq 0 ]
          [ "$output" = "v=<leaked>" ]
          ;;
        *)
          [ "$status" -eq 1 ]
          [ "$output" = "" ]
          ;;
      esac
      ;;
    busybox)
      # No shopt at all, and ash clears -e in command substitutions.
      [[ "$output" == *"v=<leaked>"* ]]
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh)
      # No shopt; these shells keep -e in $(...) anyway.
      [ "$status" -eq 1 ]
      [[ "$output" != *"v=<"* ]]
      ;;
    *) false ;;
  esac
}
