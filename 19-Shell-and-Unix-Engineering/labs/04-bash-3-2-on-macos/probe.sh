#!/bin/sh
# probe.sh MODE: show what the running bash release does where bash 3.2 differs from bash 4.0+.
# Every bash-only construct sits in a single-quoted eval string, so every shell can parse this
# file; a shell that is not bash prints "not-bash" and exits 3.
#
# Modes:
#   features      one NAME=yes|no line per feature, oldest feature first
#   assoc         declare -A, then two string keys (bash 3.2 maps both keys to index 0)
#   nounset       "${a[@]}" of an empty array under set -u (fatal before bash 4.4)
#   nounset-idiom the ${a[@]+"${a[@]}"} workaround, for an empty and a two-element array
#   fd-fatal      exec {fd}>/dev/null at top level (bash 3.2 exits 127)
#   echo-posix    echo with a backslash and with -n, in POSIX mode
#   export-func   how export -f encodes a function in the environment
#   shellshock    run $BASH with a CVE-2014-6271 environment and report what it executed

# shellcheck disable=SC2016 # the single-quoted strings are bash code for eval, expanded later
if [ -z "${BASH_VERSION-}" ]; then
  echo not-bash
  exit 3
fi

# has NAME CODE: run CODE in a subshell, so a syntax error or a fatal error stays contained;
# print NAME=yes when it succeeds and NAME=no otherwise.
has() {
  if (eval "$2") >/dev/null 2>&1; then
    printf '%s=yes\n' "$1"
  else
    printf '%s=no\n' "$1"
  fi
}

features() {
  # bs is one backslash, built at run time so this file stays free of a literal \u escape.
  bs=$(printf '\134')
  # Present in bash 3.2 (3.0 or 3.1 per NEWS).
  has regex '[[ ab =~ ^a ]]'
  has pipefail 'set -o pipefail'
  has appendop 'x=a; x+=b; [ "$x" = ab ]'
  has printfv 'printf -v x %s a; [ "$x" = a ]'
  has comsubparen 'x=$(case a in (a) echo y;; esac); [ "$x" = y ]'
  # bash 4.0
  has assoc 'declare -A m'
  has mapfile 'mapfile -t a </dev/null'
  has casemod 'x=Ab; [ "${x,,}" = ab ]'
  has declarel 'declare -l x=AB; [ "$x" = ab ]'
  has fallthrough 'case a in a) : ;& b) : ;; esac'
  has globstar 'shopt -s globstar'
  has pipeamp 'true |& true'
  has appendall 'true &>>/dev/null'
  has zeropad 'set -- {01..03}; [ "$1" = 01 ]'
  has readfrac 'read -t 0.5 x <<< y && [ "$x" = y ]'
  has bashpid '[ -n "${BASHPID-}" ]'
  has comsubcase 'x=$(case a in a) echo y;; esac); [ "$x" = y ]'
  # bash 4.1
  has fdvar 'exec {fd}>/dev/null && [ "$fd" -ge 10 ]'
  has printfvarr 'printf -v "a[1]" %s v && [ "${a[1]}" = v ]'
  has readN 'read -N 2 x <<< abc; [ "$x" = ab ]'
  # bash 4.2
  has uescape "x=\$'${bs}u0041'; [ \"\$x\" = A ]"
  has testv 'x=1; [[ -v x ]]'
  has negsub 'a=(1 2 3); [ "${a[-1]}" = 3 ]'
  has neglen 'x=abcd; [ "${x:1:-1}" = bc ]'
  has printfT 'printf "%(%Y)T" -1'
  has lastpipe 'shopt -s lastpipe'
  has declareg 'f() { declare -g gv=1; }; f; [ "$gv" = 1 ]'
  # bash 4.3
  has nameref 'x=1; declare -n r=x; [ "$r" = 1 ]'
  has waitn '( exit 7 ) & wait -n; [ "$?" -eq 7 ]'
  # bash 4.4
  has atQ 'x=a; [ "${x@Q}" = "'"'"'a'"'"'" ]'
  has nounsetarr 'set -u; a=(); : "${a[@]}"'
  has printfvempty 'v=old; printf -v v ""; [ -z "$v" ]'
  has inherit 'shopt -s inherit_errexit'
  has localdash 'f() { local -; set -f; }; f; case $- in *f*) false ;; *) true ;; esac'
  # bash 5.0
  has epoch '[ -n "${EPOCHSECONDS-}" ]'
  # bash 5.1
  has srandom '[ -n "${SRANDOM-}" ]'
  has waitp '( exit 3 ) & wait -n -p pid; [ "$pid" -gt 0 ]'
  # bash 5.3
  has funsub 'x=${ echo hi; }; [ "$x" = hi ]'
}

case ${1-} in
  features)
    features
    ;;
  assoc)
    eval 'declare -A m 2>/dev/null
m[foo]=1
m[bar]=2
printf "foo=%s bar=%s count=%s\n" "${m[foo]}" "${m[bar]}" "${#m[@]}"'
    ;;
  nounset)
    # Runs in the current shell on purpose: the point is that the error ends the script.
    eval 'set -u; a=(); printf "<%s>" "${a[@]}"; echo " reached"'
    ;;
  nounset-idiom)
    eval 'set -u
count() { echo "$#"; }
a=()
n0=$(count ${a[@]+"${a[@]}"})
a=(x "")
n2=$(count ${a[@]+"${a[@]}"})
echo "empty=$n0 two=$n2"'
    ;;
  fd-fatal)
    eval 'exec {fd}>/dev/null'
    # shellcheck disable=SC2154 # fd is assigned by the {fd}> redirection inside the eval
    if [ "$fd" -ge 10 ]; then
      echo "survived fd>=10"
    else
      echo "survived fd=$fd"
    fi
    ;;
  echo-posix)
    eval 'set -o posix; echo "a\nb"; echo -n x; echo'
    ;;
  export-func)
    eval 'f() { echo hi; }; export -f f'
    if env | grep '^BASH_FUNC_f%%=' >/dev/null; then
      echo 'encoding=BASH_FUNC_f%%'
    elif env | grep '^f=() {' >/dev/null; then
      echo 'encoding=f'
    else
      echo 'encoding=unknown'
    fi
    ;;
  shellshock)
    env 'x=() { :; }; echo VULNERABLE' "$BASH" -c 'echo body'
    ;;
  *)
    echo "usage: probe.sh features|assoc|nounset|nounset-idiom|fd-fatal|echo-posix|export-func|shellshock" >&2
    exit 64
    ;;
esac
