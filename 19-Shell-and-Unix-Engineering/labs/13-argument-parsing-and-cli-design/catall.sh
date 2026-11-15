#!/bin/sh
# catall.sh MODE DIR - print every file in DIR with cat, three ways, to show option injection:
#   naive     cat *     a file named -n becomes an option to cat and is never read
#   dashdash  cat -- *  -- ends cat's options, so every name is an operand
#   dotslash  cat ./*   every name starts with ./, so none can look like an option
# /bin/cat is named explicitly because some shells (Alpine's mksh) have a cat builtin.
mode=$1
cd -- "$2" || exit 1
case $mode in
  naive)
    # shellcheck disable=SC2035 # the unguarded glob is the bug this mode demonstrates
    exec /bin/cat *
    ;;
  dashdash) exec /bin/cat -- * ;;
  dotslash) exec /bin/cat ./* ;;
  *)
    printf 'catall.sh: unknown mode: %s\n' "$mode" >&2
    exit 2
    ;;
esac
