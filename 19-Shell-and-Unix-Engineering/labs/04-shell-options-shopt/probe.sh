#!/usr/bin/env bash
# probe.sh - probe the bash shopt builtin: statuses, output formats, scope, startup, and coupling.
# Usage: probe.sh MODE [DIR]
#   defaults  how many options this bash has, and which are on in a script
#   statuses  exit status of every way to call shopt
#   formats   the two output formats and the width of the listing column
#   save      saving and restoring one option under set -e
#   scope     functions, subshells, and local -
#   startup   how a child bash gets (or does not get) the parent's options
#   parse     extglob is read by the parser, so it must be set before the line is read (DIR)
#   globs     nullglob, dotglob, and globstar change what a pattern expands to (DIR)
#   coupling  other state that moves shopt options, or that shopt moves
#   bridge    shopt -o, the door to the set -o table
#   posixorder  whether +O beats POSIX mode, for each way of entering POSIX mode
#   shared    two option names that bash 5.3 backs with one variable
# Every mode prints key=value lines.
# The matrix runs this file under every shell, so it uses POSIX syntax only and keeps bash-only
# syntax inside eval strings; a shell with no shopt builtin reports shopt=absent and stops.

mode=${1-}
dir=${2-}

if ! command -v shopt >/dev/null 2>&1; then
  printf 'shopt=absent\n'
  exit 0
fi

# show KEY NAME: print KEY=the reusable form of option NAME.
# shopt -p returns 1 for an option that is off, so the status is deliberately dropped.
show() {
  value=$(shopt -p "$2") || true
  printf '%s=%s\n' "$1" "$value"
}

# child KEY ARG...: run the bash under test with ARG... and print KEY=its standard output,
# or KEY=status:N when it printed nothing.
child() {
  key=$1
  shift
  status=0
  value=$("$BASH" "$@" 2>/dev/null) || status=$?
  printf '%s=%s\n' "$key" "${value:-status:$status}"
}

# flags KEY: print KEY=the letters E and T that are present in $-.
flags() {
  letters=
  case $- in
    *E*) letters=E ;;
    *) ;;
  esac
  case $- in
    *T*) letters=${letters}T ;;
    *) ;;
  esac
  printf '%s=%s\n' "$1" "${letters:-none}"
}

case $mode in
  defaults)
    if shopt -qo posix; then
      printf 'posix=on\n'
    else
      printf 'posix=off\n'
    fi
    listing=$(shopt)
    total=0
    on=
    while read -r name state; do
      total=$((total + 1))
      case $state in
        on) on="$on $name" ;;
        *) ;;
      esac
    done <<EOF_LISTING
$listing
EOF_LISTING
    printf 'total=%s\n' "$total"
    printf 'on=%s\n' "${on# }"
    ;;

  statuses)
    shopt -u nullglob dotglob
    shopt -q sourcepath
    printf 'query_on=%s\n' "$?"
    shopt -q nullglob
    printf 'query_off=%s\n' "$?"
    shopt -q sourcepath nullglob
    printf 'query_mixed=%s\n' "$?"
    shopt -q no_such_option 2>/dev/null
    printf 'query_invalid=%s\n' "$?"
    shopt -s no_such_option dotglob 2>/dev/null
    printf 'set_mixed=%s\n' "$?"
    show set_mixed_applied dotglob
    shopt -s -u nullglob 2>/dev/null
    printf 'set_and_unset=%s\n' "$?"
    shopt -Z 2>/dev/null
    printf 'bad_flag=%s\n' "$?"
    shopt -s login_shell
    printf 'readonly_set=%s\n' "$?"
    show readonly_value login_shell
    ;;

  formats)
    shopt -u nullglob
    tab=$(printf '\t')
    line=$(shopt nullglob) || true
    name=${line%%"$tab"*}
    printf 'name_width=%s\n' "${#name}"
    printf 'name=%s\n' "${name%% *}"
    printf 'state=%s\n' "${line##*"$tab"}"
    show reusable_off nullglob
    shopt -s nullglob
    show reusable_on nullglob
    listed=$(shopt -s) || true
    case $listed in
      *nullglob*) printf 'set_list_has_nullglob=yes\n' ;;
      *) printf 'set_list_has_nullglob=no\n' ;;
    esac
    listed=$(shopt -u) || true
    case $listed in
      *nullglob*) printf 'unset_list_has_nullglob=yes\n' ;;
      *) printf 'unset_list_has_nullglob=no\n' ;;
    esac
    ;;

  save)
    shopt -u nullglob
    # The naive idiom: under set -e, an assignment whose command substitution fails ends the shell,
    # and shopt -p fails whenever the option it prints is off.
    reached=$(
      set -e
      saved=$(shopt -p nullglob)
      printf 'yes %s\n' "$saved"
    )
    printf 'naive_status=%s\n' "$?"
    printf 'naive_reached_end=%s\n' "${reached:-no}"
    # The working idiom: drop the status, keep the text.
    saved=$(shopt -p nullglob) || true
    printf 'saved=%s\n' "$saved"
    shopt -s nullglob
    show during nullglob
    eval "$saved"
    show restored nullglob
    ;;

  scope)
    shopt -u nullglob shift_verbose
    set_in_function() {
      shopt -s nullglob
    }
    set_in_function
    show after_function nullglob
    (
      show inside_subshell nullglob
      shopt -u nullglob
      show subshell_after_unset nullglob
    )
    show parent_after_subshell nullglob
    shopt -u nullglob
    with_local_dash() {
      local - 2>/dev/null
      set -f
      shopt -s nullglob shift_verbose
    }
    with_local_dash
    case $- in
      *f*) printf 'local_dash_noglob=leaked\n' ;;
      *) printf 'local_dash_noglob=restored\n' ;;
    esac
    set +f
    show local_dash_nullglob nullglob
    show local_dash_shift_verbose shift_verbose
    ;;

  startup)
    shopt -u nullglob
    printf 'invoked_as=%s\n' "${BASH##*/}"
    child dash_O -O nullglob -c 'shopt -p nullglob'
    child plus_O +O extquote -c 'shopt -p extquote'
    # sourcepath is one of the options POSIX mode owns: a bash named sh enters POSIX mode after the
    # -O list has been applied, and turns it back on.
    child plus_O_posix_owned +O sourcepath -c 'shopt -p sourcepath'
    child bad_O -O no_such_option -c 'echo ran'
    shopt -s nullglob
    child plain -c 'shopt -p nullglob'
    export BASHOPTS
    child exported -c 'shopt -p nullglob'
    child privileged -p -c 'shopt -p nullglob'
    export -n BASHOPTS
    value=$(env BASHOPTS=no_such_option:dotglob "$BASH" -c 'shopt -p dotglob' 2>/dev/null) || true
    printf 'env_with_unknown_name=%s\n' "$value"
    if (eval 'BASHOPTS=nullglob') 2>/dev/null; then
      printf 'bashopts_assignable=yes\n'
    else
      printf 'bashopts_assignable=no\n'
    fi
    # A script with no #! line: bash runs it itself after execve fails with ENOEXEC,
    # and resets every shopt option first.
    if [ -n "$dir" ]; then
      printf 'shopt -p nullglob\n' >"$dir/no-shebang"
      chmod +x "$dir/no-shebang"
      value=$("$dir/no-shebang") || true
      printf 'no_shebang_child=%s\n' "$value"
      # shellcheck disable=SC1091 # the file is written a few lines above; nothing to follow
      value=$(. "$dir/no-shebang") || true
      printf 'sourced=%s\n' "$value"
    fi
    ;;

  parse)
    cd "$dir" || exit 1
    shopt -u extglob
    # One line: the parser reads the whole line before shopt runs, so the pattern is a syntax error.
    if (eval 'shopt -s extglob; echo @(a|b).txt') >/dev/null 2>&1; then
      printf 'same_line=parsed\n'
    else
      printf 'same_line=syntax-error\n'
    fi
    # Two parses: the option is on by the time the second string is read.
    shopt -s extglob
    eval 'set -- @(a|b).txt'
    printf 'next_parse=%s\n' "$*"
    # Parsed with extglob on, run with it off: the word parses, but the matcher no longer treats it
    # as an extended pattern, so the expansion finds nothing and leaves the text alone.
    eval 'matcher() { set -- @(a|b).txt; printf "%s\n" "$*"; }'
    shopt -u extglob
    value=$(matcher) || true
    printf 'parsed_on_run_off=%s\n' "$value"
    # An exported function is re-parsed by the child when it imports the environment.
    shopt -s extglob
    eval 'classify() { case $1 in @(a|b)) echo yes ;; *) echo no ;; esac; }'
    export -f classify
    child import_plain -c 'classify a'
    export BASHOPTS
    child import_bashopts -c 'classify a'
    export -n BASHOPTS
    child import_dash_O -O extglob -c 'classify a'
    ;;

  globs)
    cd "$dir" || exit 1
    LC_ALL=C
    export LC_ALL
    shopt -u nullglob dotglob
    set -- *.none
    printf 'default_nomatch=%s:%s\n' "$#" "$1"
    set -- [a-c] [A-C]
    printf 'default_bracket_words=%s\n' "$#"
    set -- *
    printf 'default_star=%s\n' "$*"
    eval 'list=(x y); unset list[0]; printf "default_unset_left=%s\n" "${#list[@]}"'
    shopt -s nullglob
    set -- *.none
    printf 'nullglob_nomatch=%s\n' "$#"
    set -- [a-c] [A-C]
    printf 'nullglob_bracket_words=%s\n' "$#"
    eval 'list=(x y); unset list[0]; printf "nullglob_unset_left=%s\n" "${#list[@]}"'
    shopt -u nullglob
    shopt -s dotglob
    set -- *
    printf 'dotglob_star=%s\n' "$*"
    shopt -u dotglob
    shopt -s globstar 2>/dev/null
    printf 'globstar_status=%s\n' "$?"
    set -- **/*.txt
    printf 'double_star=%s\n' "$*"
    ;;

  coupling)
    shopt -u dotglob
    show dotglob_before dotglob
    GLOBIGNORE=no-such-name
    show dotglob_with_globignore dotglob
    case ":${BASHOPTS-}:" in
      *:dotglob:*) printf 'bashopts_lists_dotglob=yes\n' ;;
      *) printf 'bashopts_lists_dotglob=no\n' ;;
    esac
    shopt -s dotglob
    unset GLOBIGNORE
    show dotglob_after_unset_globignore dotglob
    set -E
    flags trace_before
    shopt -s extdebug
    flags trace_extdebug_on
    shopt -u extdebug
    flags trace_extdebug_off
    case ":$SHELLOPTS:" in
      *:interactive-comments:*) printf 'shellopts_before=listed\n' ;;
      *) printf 'shellopts_before=missing\n' ;;
    esac
    shopt -u interactive_comments
    case ":$SHELLOPTS:" in
      *:interactive-comments:*) printf 'shellopts_after=listed\n' ;;
      *) printf 'shellopts_after=missing\n' ;;
    esac
    shopt -s interactive_comments
    shopt -s compat31 compat32 2>/dev/null
    show compat31 compat31
    value=$(shopt -p compat32 2>/dev/null) || true
    printf 'compat32=%s\n' "${value:-invalid}"
    printf 'bash_compat_set=%s\n' "${BASH_COMPAT-unset}"
    shopt -u compat31 compat32 2>/dev/null
    printf 'bash_compat_cleared=%s\n' "${BASH_COMPAT-unset}"
    ;;

  bridge)
    set +f
    shopt -u nullglob
    value=$(shopt -po noglob) || true
    printf 'print_o=%s\n' "$value"
    shopt -so noglob
    case $- in
      *f*) printf 'set_o_reaches_set=yes\n' ;;
      *) printf 'set_o_reaches_set=no\n' ;;
    esac
    shopt -qo noglob
    printf 'query_o=%s\n' "$?"
    shopt -uo noglob
    shopt -s noglob 2>/dev/null
    printf 'set_without_o=%s\n' "$?"
    shopt -so nullglob 2>/dev/null
    printf 'set_o_invalid=%s\n' "$?"
    shopt -qo nullglob 2>/dev/null
    printf 'query_o_invalid=%s\n' "$?"
    shopt -s nullglob
    [ -o nullglob ]
    printf 'test_o_sees_shopt=%s\n' "$?"
    ;;

  posixorder)
    # Three doors into POSIX mode, two precedences: --posix is applied before the -O queue, while
    # an imported POSIXLY_CORRECT and the name sh re-enter POSIX mode after it.
    printf 'invoked_as=%s\n' "${BASH##*/}"
    child flag_posix --posix +O sourcepath -c 'shopt -p sourcepath'
    value=$(env POSIXLY_CORRECT=y "$BASH" +O sourcepath -c 'shopt -p sourcepath' 2>/dev/null) || true
    printf 'env_posix=%s\n' "${value:-none}"
    ;;

  shared)
    # assoc_expand_once arrived in bash 5.0; 5.3 added array_expand_once and points both table
    # entries at the same int, so changing either name changes the other.
    shopt -u assoc_expand_once 2>/dev/null
    shopt -s assoc_expand_once 2>/dev/null
    printf 'set_assoc=%s\n' "$?"
    value=$(shopt -p array_expand_once 2>/dev/null) || true
    printf 'array_after_assoc_on=%s\n' "${value:-invalid}"
    shopt -u array_expand_once 2>/dev/null
    value=$(shopt -p assoc_expand_once 2>/dev/null) || true
    printf 'assoc_after_array_off=%s\n' "${value:-invalid}"
    ;;

  *)
    printf 'usage: probe.sh defaults|statuses|formats|save|scope|startup|parse|globs|coupling|bridge|posixorder|shared [DIR]\n' >&2
    exit 2
    ;;
esac
