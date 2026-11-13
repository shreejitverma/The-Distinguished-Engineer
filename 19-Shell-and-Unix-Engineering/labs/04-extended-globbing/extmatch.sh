#!/bin/sh
# extmatch.sh: turn on ksh-style extended patterns with this shell's own switch, then
# print whether each STRING matches its PATTERN in a case statement.
# Usage: extmatch.sh PATTERN:STRING...
# Prints "mode=shopt" (bash), "mode=setopt" (zsh), "mode=native" (ksh93, mksh), or "mode=none",
# then one "PATTERN:STRING R" line per argument with R = 1 (match) or 0; exits 3 for mode=none.
# Example: extmatch.sh '+(ab):abab' '!(ab|cd):ab'  ->  mode=shopt / +(ab):abab 1 / !(ab|cd):ab 0

enable_ext() {
  if command -v shopt >/dev/null 2>&1; then
    # shellcheck disable=SC3044 # bash-only builtin, reached only when command -v finds it
    shopt -s extglob && ext_mode=shopt
  elif command -v setopt >/dev/null 2>&1; then
    setopt kshglob && ext_mode=setopt
  else
    ext_mode=native
  fi
  # The parser must see @( ) after the switch is on, so the probe text goes through eval.
  if ! (eval 'case ab in @(ab|cd)) exit 0 ;; *) exit 1 ;; esac') 2>/dev/null; then
    ext_mode=none
  fi
}

enable_ext
printf 'mode=%s\n' "$ext_mode"
[ "$ext_mode" != none ] || exit 3

for pair in "$@"; do
  pat=${pair%%:*}
  # shellcheck disable=SC2034 # str is read by the eval text below
  str=${pair#*:}
  r=none
  # The pattern is pasted into the case text so it is parsed as a literal pattern, not
  # expanded from a variable (shells disagree about patterns that come from expansions).
  eval "case \$str in $pat) r=1 ;; *) r=0 ;; esac"
  printf '%s %s\n' "$pair" "$r"
done
