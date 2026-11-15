#!/bin/sh
# compat-probe: show what the running bash release and its compatibility level change.
# Usage: compat-probe.sh MODE [ARG]
#   versinfo           "fields=N match=yes|no readonly=yes|no" for BASH_VERSINFO
#   regex [LEVEL]      set BASH_COMPAT=LEVEL, then say whether [[ abc =~ "$re" ]] with re=a.c
#                      matched as a regex ("regex") or as a literal string ("literal")
#   regex-env LEVEL    the same, but LEVEL arrives in the initial environment of a new bash
#   herestring [LEVEL] set BASH_COMPAT=LEVEL, then say whether a here-string is a pipe or a file
#   compat-error LEVEL "rejected" if assigning BASH_COMPAT=LEVEL prints an error, else "accepted"
#   patsub [off]       print ${s/b/[&]} for s=abc, after shopt -u patsub_replacement if "off"
#   nofork             compare $(...) with the bash 5.3 ${ ...; } form on a side effect
# Prints "not-bash" under any other shell. All bash syntax sits in single-quoted eval strings,
# so shells and releases that cannot parse it never see it outside eval.
# shellcheck disable=SC2016 # the single-quoted code is expanded later, by eval, on purpose

# shellcheck disable=SC3028 # BASH_VERSION is how a script asks whether it runs under bash
if [ -z "${BASH_VERSION-}" ]; then
  echo not-bash
  exit 0
fi

mode=${1-}
case $mode in
  versinfo)
    eval '
      n=${#BASH_VERSINFO[@]}
      v="${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}.${BASH_VERSINFO[2]}"
      case $BASH_VERSION in "$v"*) m=yes ;; *) m=no ;; esac
      if (BASH_VERSINFO[0]=99) 2>/dev/null; then r=no; else r=yes; fi
      echo "fields=$n match=$m readonly=$r"'
    ;;
  regex)
    eval '
      if [ -n "${2-}" ]; then { BASH_COMPAT=$2; } 2>/dev/null; fi
      re="a.c"
      if [[ abc =~ "$re" ]]; then echo regex; else echo literal; fi'
    ;;
  regex-env)
    # shellcheck disable=SC3028 # BASH names the running bash binary, so the child is the same release
    exec env "BASH_COMPAT=$2" "$BASH" "$0" regex
    ;;
  herestring)
    eval '
      if [ -n "${2-}" ]; then { BASH_COMPAT=$2; } 2>/dev/null; fi
      if [ -p /dev/stdin ] <<< x; then echo pipe; else echo file; fi'
    ;;
  compat-error)
    err=$({ BASH_COMPAT=$2; } 2>&1)
    if [ -n "$err" ]; then echo rejected; else echo accepted; fi
    ;;
  patsub)
    eval '
      if [ "${2-}" = off ]; then
        shopt -u patsub_replacement 2>/dev/null || { echo no-option; exit 0; }
      fi
      s=abc
      r=${s/b/[&]}
      printf "%s\n" "$r"'
    ;;
  nofork)
    eval 'n=0; x=$(n=1; echo hi); echo "comsub n=$n x=$x"'
    out=$(eval 'n=0; x=${ n=1; echo hi; }; [ "$x" = hi ] && echo "nofork n=$n x=$x"' 2>/dev/null) \
      || out="nofork unsupported"
    printf '%s\n' "$out"
    ;;
  *)
    echo "usage: compat-probe.sh versinfo|regex|regex-env|herestring|compat-error|patsub|nofork [ARG]" >&2
    exit 2
    ;;
esac
