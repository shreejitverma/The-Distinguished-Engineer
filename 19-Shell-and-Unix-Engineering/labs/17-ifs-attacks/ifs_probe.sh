#!/bin/sh
# ifs_probe.sh MODE [ARG...]: deterministic probes for IFS attacks and field-splitting hazards.
# Every mode computes what it prints from the live shell; the bats file asserts the output.
#
# Modes:
#   state                  the shell's own IFS (hex) and how many fields an unquoted /bin/echo splits into
#   import VALUE SHELL...  start SHELL... with IFS=VALUE in the environment and run `state` in it
#   fields SEP PAYLOAD     split an unquoted PAYLOAD with IFS=SEP and list the fields
#   literal                IFS=/ splits an unquoted expansion but never literal text
#   bypass                 `${IFS}` and `$IFS$9` as space substitutes, and the null-IFS failure
#   native_split           whether the unquoted expansion of `a b` splits (no emulation applied)
#   filter PAYLOAD         a "no whitespace" filter in front of an eval sink
#   filter_safe PAYLOAD    the same filter with a data-only sink (no eval)
#   leak                   an IFS assignment that leaks out of a function, versus a subshell and `read`
#   wrapper                `eval "$*"` after a leaked IFS=';'
#   join                   "$*" joined with a custom, null, and unset IFS
#   readesc                `IFS=: read` with and without -r on a backslash-escaped delimiter
#   record                 a comma record shifted by an attacker-supplied comma
#   restore                saving and restoring IFS through a scalar when IFS was unset
set -e

mode="${1:-}"
shift || true

# Native zsh does not split unquoted expansions; run every mode but native_split in sh emulation.
case ${ZSH_VERSION-} in
  ?*) [ "$mode" = native_split ] || emulate sh ;;
  *) ;;
esac

hexof() {
  printf '%s' "$1" | od -An -tx1 | tr -d ' \n'
}

# flat TEXT: print TEXT with each newline replaced by a vertical bar.
flat() {
  printf '%s' "$1" | tr '\n' '|'
}

# show_fields: print the field count, then each field in angle brackets, on one line.
show_fields() {
  printf '%d:' "$#"
  if [ "$#" -gt 0 ]; then
    printf '<%s>' "$@"
  fi
  printf '\n'
}

case "$mode" in
  state)
    ifs_hex=$(hexof "$IFS")
    printf 'ifs_hex: %s\n' "$ifs_hex"
    path=/bin/echo
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $path
    printf 'path_fields: %d\n' "$#"
    ;;

  import)
    value="${1:-}"
    shift
    env "IFS=$value" "$@" "$0" state
    ;;

  fields)
    sep="${1:-}"
    payload="${2:-}"
    IFS="$sep"
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $payload
    show_fields "$@"
    ;;

  literal)
    path=/bin/echo
    IFS=/
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $path
    printf 'expansion: '
    show_fields "$@"
    set -- /bin/echo
    printf 'literal: '
    show_fields "$@"
    ;;

  bypass)
    # Each eval string below has no space, tab, or newline in the source text.
    # The two shell variables are expanded by the shell under test, not by this script.
    out=$(eval 'echo${IFS}a${IFS}b' 2>/dev/null) && st=0 || st=$?
    printf 'braced: <%s> status=%d\n' "$out" "$st"
    out=$(eval 'echo$IFS$9a' 2>/dev/null) && st=0 || st=$?
    printf 'dollar9: <%s> status=%d\n' "$out" "$st"
    IFS=
    out=$(eval 'echo${IFS}a${IFS}b' 2>/dev/null) && st=0 || st=$?
    printf 'null_ifs: <%s> status=%d\n' "$out" "$st"
    unset IFS
    out=$(eval 'echo${IFS}a${IFS}b' 2>/dev/null) && st=0 || st=$?
    printf 'unset_ifs: <%s> status=%d\n' "$out" "$st"
    ;;

  native_split)
    v='a b'
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $v
    printf 'unquoted_fields: %d\n' "$#"
    out=$(eval 'echo${IFS}hi' 2>/dev/null) && st=0 || st=$?
    printf 'ifs_word: <%s> status=%d\n' "$out" "$st"
    ;;

  filter)
    payload="${1:-}"
    case $payload in
      *[[:space:]]*)
        printf 'rejected\n'
        exit 2
        ;;
      *) ;;
    esac
    # The sink is deliberately an eval; the filter is the only defense.
    eval "echo $payload" 2>/dev/null
    ;;

  filter_safe)
    payload="${1:-}"
    case $payload in
      *[[:space:]]*)
        printf 'rejected\n'
        exit 2
        ;;
      *) ;;
    esac
    printf '%s\n' "$payload"
    ;;

  leak)
    v='a b:c'
    parse() {
      IFS=:
      # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
      set -- $1
    }
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $v
    printf 'before: '
    show_fields "$@"
    # A caller that scopes the change in a subshell does not leak it.
    (parse "$v")
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $v
    printf 'after_subshell: '
    show_fields "$@"
    # IFS=: before read applies to that one command only.
    IFS=: read -r first second <<EOF
x:y
EOF
    printf 'read: <%s><%s>\n' "$first" "$second"
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $v
    printf 'after_read: '
    show_fields "$@"
    # The same function called in the current shell leaks.
    parse "$v"
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $v
    printf 'after_leak: '
    show_fields "$@"
    ;;

  wrapper)
    marker() {
      printf 'MARKER\n'
    }
    wrap() {
      eval "$*"
    }
    out=$(wrap echo marker 2>/dev/null) || true
    flat_out=$(flat "$out")
    printf 'default: %s\n' "$flat_out"
    IFS=';'
    out=$(wrap echo marker 2>/dev/null) || true
    flat_out=$(flat "$out")
    printf 'leaked: %s\n' "$flat_out"
    ;;

  join)
    set -- a 'b c' d
    IFS=';'
    printf 'custom: <%s>\n' "$*"
    IFS=
    printf 'null: <%s>\n' "$*"
    unset IFS
    printf 'unset: <%s>\n' "$*"
    ;;

  readesc)
    printf 'a\\:b:c\n' | {
      IFS=: read -r user rest
      printf 'with_r: <%s><%s>\n' "$user" "$rest"
    }
    printf 'a\\:b:c\n' | {
      # shellcheck disable=SC2162 # the missing -r is the behavior under test
      IFS=: read user rest
      printf 'without_r: <%s><%s>\n' "$user" "$rest"
    }
    ;;

  record)
    line='bob,admin,guest'
    printf '%s\n' "$line" | {
      IFS=, read -r name role
      printf 'two_vars: name=<%s> role=<%s>\n' "$name" "$role"
    }
    printf '%s\n' "$line" | {
      IFS=, read -r name role extra
      printf 'three_vars: name=<%s> role=<%s> extra=<%s>\n' "$name" "$role" "$extra"
    }
    ;;

  restore)
    v='a b'
    unset IFS
    saved=$IFS
    IFS=:
    IFS=$saved
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $v
    printf 'naive_restore: '
    show_fields "$@"
    unset IFS
    saved_set=${IFS+y}
    saved=${IFS-}
    IFS=:
    if [ "$saved_set" = y ]; then
      IFS=$saved
    else
      unset IFS
    fi
    # shellcheck disable=SC2086,SC2248 # the unquoted expansion is the behavior under test
    set -- $v
    printf 'exact_restore: '
    show_fields "$@"
    ;;

  *)
    printf 'usage: %s MODE [ARG...] (see the header)\n' "$0" >&2
    exit 1
    ;;
esac
