#!/usr/bin/env bats
# shopt.bats - tests for the bash shopt builtin, and for what the other shells do in its place.
# shopt exists only in bash, so every probe test has two arms: exact values for bash, split by
# release where the releases differ, and "shopt=absent" for every other family.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  FAILGLOB="$BATS_TEST_DIRNAME/failglob-abort.sh"
}

# make_fixture: create the directory tree the glob tests expand in, and print its path.
make_fixture() {
  local dir="$BATS_TEST_TMPDIR/fixture"
  mkdir -p "$dir/s/t"
  touch "$dir/a.txt" "$dir/b.txt" "$dir/.hidden" "$dir/s/mid.txt" "$dir/s/t/deep.txt"
  printf '%s\n' "$dir"
}

# bash_vernum: print the bash under test as MAJOR * 100 + MINOR, for example 302 or 503.
bash_vernum() {
  lab_shell_words
  # shellcheck disable=SC2016 # this expansion belongs to the shell under test
  "${LAB_SHELL_WORDS[@]}" -c 'echo $((BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1]))'
}

# absent: assert that the probe found no shopt builtin and did nothing else.
absent() {
  [ "$status" -eq 0 ]
  [ "$output" = "shopt=absent" ]
}

@test "an unguarded shopt call works in bash and is command-not-found (127) in every other shell, which then keeps running" {
  # shellcheck disable=SC2016 # this expansion belongs to the shell under test
  run_lab_c 'shopt -s nullglob; echo "status=$?"; echo after'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "${#lines[@]}" -eq 2 ]
      [ "${lines[0]}" = "status=0" ]
      [ "${lines[1]}" = "after" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "${#lines[@]}" -eq 3 ]
      [[ ${lines[0]} == *shopt* ]]
      [ "${lines[1]}" = "status=127" ]
      [ "${lines[2]}" = "after" ]
      ;;
    *) false ;;
  esac
}

@test "defaults: the option count and the on-by-default set of each bash release" {
  local vernum native reported
  run_lab "$PROBE" defaults
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      vernum=$(bash_vernum)
      case $vernum in
        302)
          [ "${lines[1]}" = "total=34" ]
          native="cmdhist extquote force_fignore hostcomplete interactive_comments progcomp promptvars sourcepath"
          ;;
        404)
          [ "${lines[1]}" = "total=48" ]
          native="cmdhist complete_fullquote extquote force_fignore hostcomplete interactive_comments progcomp promptvars sourcepath"
          ;;
        501)
          [ "${lines[1]}" = "total=53" ]
          native="checkwinsize cmdhist complete_fullquote extquote force_fignore globasciiranges hostcomplete interactive_comments progcomp promptvars sourcepath"
          ;;
        502)
          [ "${lines[1]}" = "total=57" ]
          native="checkwinsize cmdhist complete_fullquote extquote force_fignore globasciiranges globskipdots hostcomplete interactive_comments patsub_replacement progcomp promptvars sourcepath"
          ;;
        503)
          [ "${lines[1]}" = "total=59" ]
          native="checkwinsize cmdhist complete_fullquote extquote force_fignore globasciiranges globskipdots hostcomplete interactive_comments patsub_replacement progcomp promptvars sourcepath"
          ;;
        *) false ;;
      esac
      reported=${lines[2]#on=}
      case ${lines[0]} in
        posix=off)
          [ "$reported" = "$native" ]
          ;;
        posix=on)
          # POSIX mode (bash started as sh) turns on the options it owns; expand_aliases is the
          # one every release adds, so check it and compare the rest with the native set.
          [[ " $reported " == *" expand_aliases "* ]]
          reported=" $reported "
          reported=${reported/ expand_aliases / }
          reported=${reported/ inherit_errexit / }
          reported=${reported/ shift_verbose / }
          reported=${reported# }
          reported=${reported% }
          [ "$reported" = "$native" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "statuses: a query returns 1 for an off option, an invalid name fails without stopping the valid ones, and a read-only option accepts -s with status 0 and does not change" {
  run_lab "$PROBE" statuses
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 10 ]
      [ "${lines[0]}" = "query_on=0" ]
      [ "${lines[1]}" = "query_off=1" ]
      [ "${lines[2]}" = "query_mixed=1" ]
      [ "${lines[3]}" = "query_invalid=1" ]
      [ "${lines[4]}" = "set_mixed=1" ]
      [ "${lines[5]}" = "set_mixed_applied=shopt -s dotglob" ]
      [ "${lines[6]}" = "set_and_unset=1" ]
      [ "${lines[7]}" = "bad_flag=2" ]
      [ "${lines[8]}" = "readonly_set=0" ]
      [ "${lines[9]}" = "readonly_value=shopt -u login_shell" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "formats: shopt -p prints a reusable command, and the listing column is 15 wide before bash 5.3 and 20 wide from 5.3" {
  local vernum
  run_lab "$PROBE" formats
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      vernum=$(bash_vernum)
      case $vernum in
        302 | 404 | 501 | 502) [ "${lines[0]}" = "name_width=15" ] ;;
        503) [ "${lines[0]}" = "name_width=20" ] ;;
        *) false ;;
      esac
      [ "${lines[1]}" = "name=nullglob" ]
      [ "${lines[2]}" = "state=off" ]
      [ "${lines[3]}" = "reusable_off=shopt -u nullglob" ]
      [ "${lines[4]}" = "reusable_on=shopt -s nullglob" ]
      [ "${lines[5]}" = "set_list_has_nullglob=yes" ]
      [ "${lines[6]}" = "unset_list_has_nullglob=no" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "save: saved=\$(shopt -p NAME) kills a set -e shell when NAME is off; dropping the status saves and restores it" {
  run_lab "$PROBE" save
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 5 ]
      [ "${lines[0]}" = "naive_status=1" ]
      [ "${lines[1]}" = "naive_reached_end=no" ]
      [ "${lines[2]}" = "saved=shopt -u nullglob" ]
      [ "${lines[3]}" = "during=shopt -s nullglob" ]
      [ "${lines[4]}" = "restored=shopt -u nullglob" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "scope: a shopt change leaks out of a function, a subshell works on a copy, and local - never covers nullglob" {
  local vernum
  run_lab "$PROBE" scope
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 7 ]
      [ "${lines[0]}" = "after_function=shopt -s nullglob" ]
      [ "${lines[1]}" = "inside_subshell=shopt -s nullglob" ]
      [ "${lines[2]}" = "subshell_after_unset=shopt -u nullglob" ]
      [ "${lines[3]}" = "parent_after_subshell=shopt -s nullglob" ]
      [ "${lines[5]}" = "local_dash_nullglob=shopt -s nullglob" ]
      vernum=$(bash_vernum)
      case $vernum in
        302)
          # No local - before bash 4.4: the set -f leaks too.
          [ "${lines[4]}" = "local_dash_noglob=leaked" ]
          [ "${lines[6]}" = "local_dash_shift_verbose=shopt -s shift_verbose" ]
          ;;
        404)
          # local - restores the set options only.
          [ "${lines[4]}" = "local_dash_noglob=restored" ]
          [ "${lines[6]}" = "local_dash_shift_verbose=shopt -s shift_verbose" ]
          ;;
        501 | 502 | 503)
          # From 5.1, local - also restores the five shopt-visible options POSIX mode owns.
          [ "${lines[4]}" = "local_dash_noglob=restored" ]
          [ "${lines[6]}" = "local_dash_shift_verbose=shopt -u shift_verbose" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "startup: a child bash gets shopt options only from -O or an exported BASHOPTS, never from the parent's memory" {
  local vernum dir
  dir=$(make_fixture)
  run_lab "$PROBE" startup "$dir"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 12 ]
      [ "${lines[1]}" = "dash_O=shopt -s nullglob" ]
      [ "${lines[2]}" = "plus_O=shopt -u extquote" ]
      case ${lines[0]} in
        invoked_as=sh) [ "${lines[3]}" = "plus_O_posix_owned=shopt -s sourcepath" ] ;;
        invoked_as=*) [ "${lines[3]}" = "plus_O_posix_owned=shopt -u sourcepath" ] ;;
        *) false ;;
      esac
      [ "${lines[4]}" = "bad_O=status:2" ]
      [ "${lines[5]}" = "plain=shopt -u nullglob" ]
      [ "${lines[7]}" = "privileged=shopt -u nullglob" ]
      [ "${lines[10]}" = "no_shebang_child=shopt -u nullglob" ]
      [ "${lines[11]}" = "sourced=shopt -s nullglob" ]
      vernum=$(bash_vernum)
      case $vernum in
        302)
          # bash 3.2 has no BASHOPTS: the name is an ordinary variable and carries nothing.
          [ "${lines[6]}" = "exported=shopt -u nullglob" ]
          [ "${lines[8]}" = "env_with_unknown_name=shopt -u dotglob" ]
          [ "${lines[9]}" = "bashopts_assignable=yes" ]
          ;;
        404 | 501 | 502 | 503)
          [ "${lines[6]}" = "exported=shopt -s nullglob" ]
          [ "${lines[8]}" = "env_with_unknown_name=shopt -s dotglob" ]
          [ "${lines[9]}" = "bashopts_assignable=no" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "parse: extglob must be on before the parser reads the pattern, and an exported function imports only under -O extglob" {
  local dir
  dir=$(make_fixture)
  run_lab "$PROBE" parse "$dir"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 6 ]
      [ "${lines[0]}" = "same_line=syntax-error" ]
      [ "${lines[1]}" = "next_parse=a.txt b.txt" ]
      [ "${lines[2]}" = "parsed_on_run_off=@(a|b).txt" ]
      [ "${lines[3]}" = "import_plain=status:127" ]
      [ "${lines[4]}" = "import_bashopts=status:127" ]
      [ "${lines[5]}" = "import_dash_O=yes" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "globs: nullglob deletes unmatched words (and the argument of unset list[0]), dotglob adds dot files, globstar needs bash 4" {
  local vernum dir
  dir=$(make_fixture)
  run_lab "$PROBE" globs "$dir"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 10 ]
      [ "${lines[0]}" = "default_nomatch=1:*.none" ]
      [ "${lines[1]}" = "default_bracket_words=2" ]
      [ "${lines[2]}" = "default_star=a.txt b.txt s" ]
      [ "${lines[3]}" = "default_unset_left=1" ]
      [ "${lines[4]}" = "nullglob_nomatch=0" ]
      [ "${lines[5]}" = "nullglob_bracket_words=0" ]
      [ "${lines[6]}" = "nullglob_unset_left=2" ]
      [ "${lines[7]}" = "dotglob_star=.hidden a.txt b.txt s" ]
      vernum=$(bash_vernum)
      case $vernum in
        302)
          # The option name is invalid, the script carries on, and ** is two ordinary stars.
          [ "${lines[8]}" = "globstar_status=1" ]
          [ "${lines[9]}" = "double_star=s/mid.txt" ]
          ;;
        404 | 501 | 502 | 503)
          [ "${lines[8]}" = "globstar_status=0" ]
          [ "${lines[9]}" = "double_star=a.txt b.txt s/mid.txt s/t/deep.txt" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "coupling: GLOBIGNORE moves dotglob behind BASHOPTS's back, shopt -u extdebug clears set -E, and the compat options exclude each other" {
  local vernum
  run_lab "$PROBE" coupling
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 13 ]
      [ "${lines[0]}" = "dotglob_before=shopt -u dotglob" ]
      [ "${lines[1]}" = "dotglob_with_globignore=shopt -s dotglob" ]
      [ "${lines[2]}" = "bashopts_lists_dotglob=no" ]
      [ "${lines[3]}" = "dotglob_after_unset_globignore=shopt -u dotglob" ]
      [ "${lines[4]}" = "trace_before=E" ]
      [ "${lines[7]}" = "shellopts_before=listed" ]
      [ "${lines[8]}" = "shellopts_after=missing" ]
      vernum=$(bash_vernum)
      case $vernum in
        302)
          [ "${lines[5]}" = "trace_extdebug_on=E" ]
          [ "${lines[6]}" = "trace_extdebug_off=E" ]
          [ "${lines[9]}" = "compat31=shopt -s compat31" ]
          [ "${lines[10]}" = "compat32=invalid" ]
          [ "${lines[11]}" = "bash_compat_set=unset" ]
          [ "${lines[12]}" = "bash_compat_cleared=unset" ]
          ;;
        404)
          [ "${lines[5]}" = "trace_extdebug_on=ET" ]
          [ "${lines[6]}" = "trace_extdebug_off=none" ]
          [ "${lines[9]}" = "compat31=shopt -u compat31" ]
          [ "${lines[10]}" = "compat32=shopt -s compat32" ]
          [ "${lines[11]}" = "bash_compat_set=unset" ]
          [ "${lines[12]}" = "bash_compat_cleared=unset" ]
          ;;
        501 | 502 | 503)
          [ "${lines[5]}" = "trace_extdebug_on=ET" ]
          [ "${lines[6]}" = "trace_extdebug_off=none" ]
          [ "${lines[9]}" = "compat31=shopt -u compat31" ]
          [ "${lines[10]}" = "compat32=shopt -s compat32" ]
          [ "${lines[11]}" = "bash_compat_set=32" ]
          # The default level is the release itself: 51, 52, or 53.
          [ "${lines[12]}" = "bash_compat_cleared=$((vernum / 100))$((vernum % 100))" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "bridge: shopt -o reaches the set -o table, test -o never sees a shopt option, and shopt -so with a bad name returns 0 from bash 4.4" {
  local vernum
  run_lab "$PROBE" bridge
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 7 ]
      [ "${lines[0]}" = "print_o=set +o noglob" ]
      [ "${lines[1]}" = "set_o_reaches_set=yes" ]
      [ "${lines[2]}" = "query_o=0" ]
      [ "${lines[3]}" = "set_without_o=1" ]
      [ "${lines[5]}" = "query_o_invalid=1" ]
      [ "${lines[6]}" = "test_o_sees_shopt=1" ]
      vernum=$(bash_vernum)
      case $vernum in
        302) [ "${lines[4]}" = "set_o_invalid=1" ] ;;
        404 | 501 | 502 | 503) [ "${lines[4]}" = "set_o_invalid=0" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "posixorder: +O beats --posix, but loses to an imported POSIXLY_CORRECT and to the name sh" {
  run_lab "$PROBE" posixorder
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 3 ]
      [ "${lines[2]}" = "env_posix=shopt -s sourcepath" ]
      case ${lines[0]} in
        invoked_as=sh) [ "${lines[1]}" = "flag_posix=shopt -s sourcepath" ] ;;
        invoked_as=*) [ "${lines[1]}" = "flag_posix=shopt -u sourcepath" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "shared: in bash 5.3 array_expand_once and assoc_expand_once are one option under two names" {
  local vernum
  run_lab "$PROBE" shared
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 3 ]
      vernum=$(bash_vernum)
      case $vernum in
        302 | 404)
          [ "${lines[0]}" = "set_assoc=1" ]
          [ "${lines[1]}" = "array_after_assoc_on=invalid" ]
          [ "${lines[2]}" = "assoc_after_array_off=invalid" ]
          ;;
        501 | 502)
          # assoc_expand_once exists, array_expand_once does not, so the unset is a no-op.
          [ "${lines[0]}" = "set_assoc=0" ]
          [ "${lines[1]}" = "array_after_assoc_on=invalid" ]
          [ "${lines[2]}" = "assoc_after_array_off=shopt -s assoc_expand_once" ]
          ;;
        503)
          [ "${lines[0]}" = "set_assoc=0" ]
          [ "${lines[1]}" = "array_after_assoc_on=shopt -s array_expand_once" ]
          [ "${lines[2]}" = "assoc_after_array_off=shopt -u assoc_expand_once" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "probe.sh rejects an unknown mode with status 2 in bash" {
  run_lab "$PROBE" no-such-mode
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 2 ]
      [[ $output == usage:* ]]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) absent ;;
    *) false ;;
  esac
}

@test "failglob-abort.sh: a failglob error discards the whole loop or function call in bash; the same file under another shell runs every line" {
  local dir
  dir=$(make_fixture)
  run_lab "$FAILGLOB" "$dir"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 8 ]
      [ "${lines[0]}" = "pass 1" ]
      [[ ${lines[1]} == *"no match: ./*.none" ]]
      [ "${lines[2]}" = "after loop status=1" ]
      [ "${lines[3]}" = "in-function" ]
      [[ ${lines[4]} == *"no match: ./*.none" ]]
      [ "${lines[5]}" = "after function status=1" ]
      [[ ${lines[6]} == *"no match: ./*.none" ]]
      [ "${lines[7]}" = "after substitution status=1 captured=[]" ]
      ;;
    zsh)
      # Native zsh has its own no-match error (NOMATCH), and it ends the script.
      [ "$status" -eq 1 ]
      [ "${#lines[@]}" -eq 3 ]
      [[ ${lines[0]} == *shopt* ]]
      [ "${lines[1]}" = "pass 1" ]
      [[ ${lines[2]} == *"no matches found: ./*.none" ]]
      ;;
    zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 14 ]
      [[ ${lines[0]} == *shopt* ]]
      [ "${lines[1]}" = "pass 1" ]
      [ "${lines[2]}" = "./*.none" ]
      [ "${lines[3]}" = "pass 1 done" ]
      [ "${lines[4]}" = "pass 2" ]
      [ "${lines[5]}" = "./*.none" ]
      [ "${lines[6]}" = "pass 2 done" ]
      [ "${lines[7]}" = "after loop status=0" ]
      [ "${lines[8]}" = "in-function" ]
      [ "${lines[9]}" = "./*.none" ]
      [ "${lines[10]}" = "after-glob-in-function" ]
      [ "${lines[11]}" = "after function status=0" ]
      [ "${lines[12]}" = "after substitution status=0 captured=[./*.none" ]
      [ "${lines[13]}" = "inside-substitution]" ]
      ;;
    *) false ;;
  esac
}

@test "each shell spells 'remove an unmatched pattern' differently, and four shells cannot say it at all" {
  local dir
  dir=$(make_fixture)
  case $(lab_shell_family) in
    bash)
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; shopt -s nullglob; set -- *.none; echo "$#"' probe "$dir"
      [ "$status" -eq 0 ]
      [ "$output" = "0" ]
      ;;
    zsh | zsh-sh)
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; setopt nullglob; set -- *.none; echo "$#"' probe "$dir"
      [ "$status" -eq 0 ]
      [ "$output" = "0" ]
      ;;
    yash)
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; set -o nullglob; set -- *.none; echo "$#"' probe "$dir"
      [ "$status" -eq 0 ]
      [ "$output" = "0" ]
      ;;
    ksh93)
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; set -- ~(N)*.none; echo "$#"' probe "$dir"
      [ "$status" -eq 0 ]
      [ "$output" = "0" ]
      ;;
    dash | busybox | posh | mksh)
      # No such option: the word stays, and set -o lists nothing with that name.
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; set -- *.none; echo "$#:$1"; set -o | grep -c -i nullglob' probe "$dir"
      [ "${lines[0]}" = "1:*.none" ]
      [ "${lines[1]}" = "0" ]
      ;;
    *) false ;;
  esac
}

@test "an option set inside a function outlives it everywhere except zsh with localoptions" {
  case $(lab_shell_family) in
    bash)
      run_lab_c 'f() { shopt -s nullglob; }; f; if shopt -q nullglob; then echo leaked; else echo restored; fi'
      [ "$status" -eq 0 ]
      [ "$output" = "leaked" ]
      ;;
    zsh | zsh-sh)
      run_lab_c 'f() { setopt localoptions nullglob; }; f; if [[ -o nullglob ]]; then echo leaked; else echo restored; fi
        g() { setopt nullglob; }; g; if [[ -o nullglob ]]; then echo leaked; else echo restored; fi'
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "restored" ]
      [ "${lines[1]}" = "leaked" ]
      ;;
    yash | busybox | posh | mksh | ksh93 | dash)
      # No shopt here; the set options behave the same way in a POSIX-style function.
      # shellcheck disable=SC2016 # this expansion belongs to the shell under test
      run_lab_c 'f() { set -f; }; f; case $- in *f*) echo leaked ;; *) echo restored ;; esac'
      [ "$status" -eq 0 ]
      [ "$output" = "leaked" ]
      ;;
    *) false ;;
  esac
}

@test "recursive ** is globstar in bash 4 and ksh93, extendedglob in yash, the default in zsh, and absent elsewhere" {
  local dir vernum
  dir=$(make_fixture)
  case $(lab_shell_family) in
    bash)
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; echo **/*.txt; shopt -s globstar 2>/dev/null; echo **/*.txt' probe "$dir"
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "s/mid.txt" ]
      vernum=$(bash_vernum)
      case $vernum in
        302) [ "${lines[1]}" = "s/mid.txt" ] ;;
        404 | 501 | 502 | 503) [ "${lines[1]}" = "a.txt b.txt s/mid.txt s/t/deep.txt" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh)
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; echo **/*.txt' probe "$dir"
      [ "$status" -eq 0 ]
      [ "$output" = "a.txt b.txt s/mid.txt s/t/deep.txt" ]
      ;;
    ksh93)
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; echo **/*.txt; set -o globstar; echo **/*.txt' probe "$dir"
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "s/mid.txt" ]
      [ "${lines[1]}" = "a.txt b.txt s/mid.txt s/t/deep.txt" ]
      ;;
    yash)
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; echo **/*.txt; set -o extendedglob; echo **/*.txt' probe "$dir"
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "s/mid.txt" ]
      [ "${lines[1]}" = "a.txt b.txt s/mid.txt s/t/deep.txt" ]
      ;;
    dash | busybox | posh | mksh)
      # shellcheck disable=SC2016 # these expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit 1; echo **/*.txt; set -o | grep -c -i globstar' probe "$dir"
      [ "${lines[0]}" = "s/mid.txt" ]
      [ "${lines[1]}" = "0" ]
      ;;
    *) false ;;
  esac
}
