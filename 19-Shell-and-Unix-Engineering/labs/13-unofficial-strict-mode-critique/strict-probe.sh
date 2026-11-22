#!/bin/sh
# strict-probe: one mode per claim of the "unofficial bash strict mode"
# (set -euo pipefail; IFS=$'\n\t'), run under the shell under test.
# Usage: strict-probe.sh MODE [ARG...]
# Modes: ifs_dollar ifs_portable ifs_glob nounset_cond nounset_or nounset_cmdsub
#        nounset_positional nounset_null pipefail_sigpipe expr_zero grep_count
#        andor_last andor_notlast brace_group cmdsub_leak
# ifs_glob reads the current directory; every other mode touches no file.

mode=${1:?usage: strict-probe.sh MODE}
shift

# strict_ifs: set IFS to <newline><tab> without $'...', so it works in every POSIX shell.
# Command substitution strips trailing newlines, so a sentinel byte protects them.
strict_ifs() {
  IFS=$(printf '\n\tx')
  IFS=${IFS%x}
}

# show_fields: print the field count of "$@" and each field in angle brackets.
show_fields() {
  printf 'fields=%s' "$#"
  for f in "$@"; do
    printf ' <%s>' "$f"
  done
  printf '\n'
}

case $mode in
  ifs_dollar)
    # The strict-mode line verbatim. It is in an eval string so that a shell
    # without $'...' (POSIX.1-2024 XCU 2.2.4) still parses this file.
    eval "IFS=\$'\\n\\t'"
    word=internet
    printf 'ifs_len=%s ' "${#IFS}"
    # shellcheck disable=SC2086,SC2248 # field splitting is the behavior under test
    show_fields $word
    ;;

  ifs_portable)
    strict_ifs
    names='Aaron Maxwell
Wayne Gretzky'
    printf 'ifs_len=%s ' "${#IFS}"
    # shellcheck disable=SC2086,SC2248 # field splitting is the behavior under test
    show_fields $names
    # read splits on IFS too: without <space> in IFS, one variable takes the whole line.
    read -r first rest <<EOF
alpha beta
EOF
    printf 'read_first=<%s> read_rest=<%s>\n' "$first" "$rest"
    # "$*" joins with the first byte of IFS, which is now <newline>.
    set -- a b
    joined=$(printf '%s' "$*" | od -An -tx1 | tr -d ' \n')
    printf 'star_join_hex=%s\n' "$joined"
    ;;

  ifs_glob)
    # Strict IFS changes splitting, not globbing: an unquoted expansion still
    # undergoes pathname expansion, and a <tab> in a file name still splits it.
    strict_ifs
    pat='*.txt'
    # shellcheck disable=SC2086,SC2248 # pathname expansion is the behavior under test
    set -- $pat
    printf 'glob '
    show_fields "$@"
    logs=$(find . -name '*.log' -print | LC_ALL=C sort)
    # shellcheck disable=SC2086,SC2248 # field splitting is the behavior under test
    set -- $logs
    printf 'find '
    show_fields "$@"
    ;;

  nounset_cond)
    # -u is an expansion error (XCU 2.8.1), not an errexit failure: an if
    # condition does not shield it, and no set -e is needed.
    set -u
    # shellcheck disable=SC2154 # undef_var is unset on purpose
    if [ -n "$undef_var" ]; then
      printf 'branch\n'
    fi
    printf 'survived\n'
    ;;

  nounset_or)
    set -u
    show_undef() {
      # shellcheck disable=SC2154 # undef_var is unset on purpose
      printf '%s\n' "$undef_var"
    }
    # shellcheck disable=SC2310 # the || context is the behavior under test
    show_undef || printf 'handled\n'
    printf 'survived\n'
    ;;

  nounset_cmdsub)
    # The expansion error ends only the command substitution's subshell
    # (XCU 2.8.1); the parent continues with a non-zero status.
    set -u
    # shellcheck disable=SC2154 # undef_var is unset on purpose
    x=$(
      printf '%s' "$undef_var"
      printf 'inner'
    )
    st=$?
    printf 'after st=%s x=<%s>\n' "$st" "$x"
    ;;

  nounset_positional)
    # Run with no arguments after MODE.
    set -u
    printf 'default=%s n=%s\n' "${1-none}" "$#"
    printf 'at=[%s]\n' "$@"
    printf 'one=%s\n' "$1"
    printf 'survived\n'
    ;;

  nounset_null)
    # -u catches unset, never empty: the Steam rm -rf "$STEAMROOT/"* shape.
    set -u
    root=
    printf 'target=%s\n' "$root/*"
    : "${root:?root is empty}"
    printf 'survived\n'
    ;;

  pipefail_sigpipe)
    # Control: without pipefail, a reader that exits early is harmless.
    control=$(
      set -e
      yes | head -n 1 >/dev/null
      printf 'survived'
    )
    printf 'control=<%s>\n' "$control"
    # shellcheck disable=SC3040 # pipefail is feature-detected
    if (set -o pipefail) 2>/dev/null; then
      # With pipefail, yes dies of SIGPIPE when head exits, and that status wins.
      # shellcheck disable=SC3040 # pipefail is feature-detected
      pf_status=$(
        set -o pipefail
        yes | head -n 1 >/dev/null
        printf '%s' "$?"
      )
      # shellcheck disable=SC3040 # pipefail is feature-detected
      pf_out=$(
        set -e
        set -o pipefail
        yes | head -n 1 >/dev/null
        printf 'survived'
      )
      printf 'pipefail=on status=%s errexit_out=<%s>\n' "$pf_status" "$pf_out"
    else
      printf 'pipefail=unsupported\n'
    fi
    ;;

  expr_zero)
    # expr exits 1 when the result is zero (POSIX expr EXIT STATUS), so a
    # counter at zero ends a set -e script; $(( )) in an assignment does not.
    expr_out=$(
      set -e
      i=0
      # shellcheck disable=SC2003 # expr exit status is the behavior under test
      expr "$i" + 0 >/dev/null
      printf 'survived'
    )
    expr_st=$?
    arith_out=$(
      set -e
      i=0
      i=$((i + 1))
      printf 'survived i=%s' "$i"
    )
    printf 'expr_st=%s expr_out=<%s> arith_out=<%s>\n' "$expr_st" "$expr_out" "$arith_out"
    ;;

  grep_count)
    # grep exits 1 for "no lines selected" and >1 for an error (POSIX grep).
    plain=$(
      set -e
      count=$(grep -c zzz /dev/null)
      printf 'count=%s' "$count"
    )
    plain_st=$?
    ortrue=$(
      set -e
      count=$(grep -c zzz /dev/null || true)
      printf 'count=%s' "$count"
    )
    # The same || true also swallows a real error (status 2) and leaves count empty.
    masked=$(
      set -e
      count=$(grep -c zzz ./no-such-file-strict-lab 2>/dev/null || true)
      printf 'count=%s' "$count"
    )
    printf 'plain_st=%s plain=<%s> ortrue=<%s> masked=<%s>\n' "$plain_st" "$plain" "$ortrue" "$masked"
    ;;

  andor_last)
    # COND && CMD as the last command: errexit ignores the failed COND, but the
    # list's status 1 becomes the script's exit status.
    set -e
    [ -f ./no-such-file-strict-lab ] && printf 'found\n'
    ;;

  andor_notlast)
    set -e
    [ -f ./no-such-file-strict-lab ] && printf 'found\n'
    printf 'done\n'
    ;;

  brace_group)
    # A failure in a non-final operand is ignored; inside a { } group that is the
    # final operand, errexit applies again.
    chain=$(
      set -e
      true && false && printf 'third'
      printf 'next'
    )
    chain_st=$?
    group=$(
      set -e
      true && {
        false
        printf 'inner'
      }
      printf 'after'
    )
    group_st=$?
    printf 'chain_st=%s chain=<%s> group_st=%s group=<%s>\n' "$chain_st" "$chain" "$group_st" "$group"
    ;;

  cmdsub_leak)
    # Strict mode never turns on inherit_errexit: native bash runs the rest of the
    # command substitution after a failure and the assignment succeeds.
    set -e
    v=$(
      false
      printf 'leaked'
    )
    printf 'v=<%s>\n' "$v"
    ;;

  *)
    printf 'strict-probe: unknown mode: %s\n' "$mode" >&2
    exit 64
    ;;
esac
