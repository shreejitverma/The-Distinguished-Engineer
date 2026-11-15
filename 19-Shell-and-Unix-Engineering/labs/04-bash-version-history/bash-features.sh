#!/bin/sh
# bash-features: report which version-gated bash features the running shell really has,
# by trying each one, never by reading a version number.
# Usage: bash-features.sh
# Prints "not-bash" under any other shell; under bash, one "NAME yes|no" line per feature.
# Every probe is a single-quoted string run by eval in a subshell, so a release that cannot
# parse the syntax fails that probe instead of the whole script (mksh and older bash parse
# far enough ahead that bare new syntax would abort the run).
# The release that introduced each feature is in bash-version-history.bats, taken from NEWS.
# shellcheck disable=SC2016 # the single-quoted code is expanded later, by eval, on purpose

# shellcheck disable=SC3028 # BASH_VERSION is how a script asks whether it runs under bash
if [ -z "${BASH_VERSION-}" ]; then
  echo not-bash
  exit 0
fi

# probe NAME CODE: print "NAME yes" if CODE succeeds in a subshell, else "NAME no".
probe() {
  if (eval "$2") >/dev/null 2>&1; then
    printf '%s yes\n' "$1"
  else
    printf '%s no\n' "$1"
  fi
}

# 3.0
probe pipefail 'set -o pipefail && ! { false | true; }'
probe regex-match '[[ abc =~ ^a.c$ ]]'
probe brace-sequence 'x=$(echo {1..3}); [ "$x" = "1 2 3" ]'
# 3.1
probe append-assign 'x=a; x+=b; [ "$x" = ab ]'
probe printf-v 'printf -v x %s ok && [ "$x" = ok ]'
# 4.0
probe assoc-array 'declare -A m && m[k]=v && m[j]=w && [ "${#m[@]}" -eq 2 ] && [ "${m[k]}" = v ]'
probe case-modify 'x=ab; [ "${x^^}" = AB ]'
probe mapfile 'mapfile -t a <<< "p" && [ "${a[0]}" = p ]'
probe coproc-keyword '[ "$(type -t coproc)" = keyword ]'
probe globstar 'shopt -s globstar'
probe pipe-stderr 'x=$( { echo e >&2; } |& cat ) && [ "$x" = e ]'
# 4.1
probe fd-variable 'exec {fd}>/dev/null && [ "$fd" -ge 10 ]'
# 4.2
probe declare-g 'f() { declare -g y=1; }; f && [ "${y-}" = 1 ]'
probe test-v 'x=; [[ -v x ]] && ! [[ -v unset_name_zz ]]'
probe negative-index-read 'a=(p q r); [ "${a[-1]}" = r ]'
probe lastpipe 'shopt -s lastpipe'
probe printf-time 'printf -v t "%(%Y)T" 0 && [ "$t" -ge 1969 ] && [ "$t" -le 1970 ]'
# 4.3
probe nameref 'x=1; declare -n r=x && r=2 && [ "$x" = 2 ]'
probe negative-index-write 'a=(p q); a[-1]=z && [ "${a[1]}" = z ]'
# 4.4
probe transform-Q 'x="a b"; [ "${x@Q}" = "'\''a b'\''" ]'
probe mapfile-d 'mapfile -t -d , a <<< "p,q" && [ "${a[0]}" = p ] && [ "${#a[@]}" -eq 2 ]'
probe local-dash 'f() { local -; set -f; }; f; case $- in *f*) false ;; *) true ;; esac'
probe inherit-errexit 'shopt -s inherit_errexit'
# 5.0
probe epochseconds '[ "${EPOCHSECONDS:-0}" -gt 1000000000 ]'
probe bash-argv0 'BASH_ARGV0=zz; [ "$0" = zz ]'
# 5.1
probe srandom 'a=$SRANDOM; b=$SRANDOM; [ -n "$a" ] && [ "$a" != "$b" ]'
probe transform-U 'x=ab; [ "${x@U}" = AB ]'
# 5.2
probe patsub-replacement 's=abc; r=${s/b/[&]}; [ "$r" = "a[b]c" ]'
probe transform-k 'declare -A m=([k]=v); a=("${m[@]@k}"); [ "${#a[@]}" -eq 2 ] && [ "${a[0]}" = k ]'
probe globskipdots 'shopt -q globskipdots'
# 5.3
probe nofork-comsub 'n=0; x=${ n=1; echo hi; }; [ "$x" = hi ] && [ "$n" = 1 ]'
probe valsub-comsub 'x=${| REPLY=v; }; [ "$x" = v ]'
probe array-expand-once 'shopt -u array_expand_once'
