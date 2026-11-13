#!/bin/sh
# extglob-files.sh DIR: expand four extended patterns as pathnames inside DIR.
# Prints the mode line (as extmatch.sh), then one line per pattern:
#   !(*.c|*.h)   @(a|b).c   +([0-9]).log   *.!(c)
# Exits 3 when the shell has no extended patterns. Sorting uses the C locale.

LC_ALL=C
export LC_ALL

enable_ext() {
  if command -v shopt >/dev/null 2>&1; then
    # shellcheck disable=SC3044 # bash-only builtin, reached only when command -v finds it
    shopt -s extglob && ext_mode=shopt
  elif command -v setopt >/dev/null 2>&1; then
    setopt kshglob && ext_mode=setopt
  else
    ext_mode=native
  fi
  if ! (eval 'case ab in @(ab|cd)) exit 0 ;; *) exit 1 ;; esac') 2>/dev/null; then
    ext_mode=none
  fi
}

enable_ext
printf 'mode=%s\n' "$ext_mode"
[ "$ext_mode" != none ] || exit 3
cd -- "$1" || exit 1

# Each pattern is in its own eval: a shell that rejects one pattern still runs the others.
eval 'echo !(*.c|*.h)'
eval 'echo @(a|b).c'
eval 'echo +([0-9]).log'
eval 'echo *.!(c)'
