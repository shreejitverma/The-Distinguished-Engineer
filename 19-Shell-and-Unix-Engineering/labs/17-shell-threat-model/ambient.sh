#!/bin/sh
# ambient.sh: show how process state inherited across execve changes what a script does.
# Each mode computes and prints what it observed; nothing here resets the inherited state.
# Usage:
#   ambient.sh ifs          field count of 'a:b' and whether IFS came from the environment
#   ambient.sh cdpath       run from a directory holding work/: where `cd work` landed
#   ambient.sh umask FILE   create FILE and print its permission string
#   ambient.sh trap         whether a trap on SIGUSR1 can run
#   ambient.sh path         what the command named `id` printed
set -u
mode=${1:-}

case $mode in
  ifs)
    v='a:b'
    # shellcheck disable=SC2086,SC2248 # field splitting is the behavior under test
    set -- $v
    if [ "$IFS" = : ]; then
      ifs=inherited
    else
      ifs=reset
    fi
    printf 'ifs=%s fields=%s\n' "$ifs" "$#"
    ;;
  cdpath)
    out=$(cd work) || exit 1
    cd work >/dev/null || exit 1
    parent=$(cd .. && pwd -P) || exit 1
    if [ -n "$out" ]; then printed=yes; else printed=no; fi
    printf 'landed=%s printed=%s\n' "${parent##*/}" "$printed"
    ;;
  umask)
    file=${2:?usage: ambient.sh umask FILE}
    printf 'secret\n' >"$file"
    perms=$(ls -ln "$file") || exit 1
    printf 'mode=%s\n' "${perms%%[ +.@]*}"
    ;;
  trap)
    trap 'printf "trap=ran\n"; exit 0' USR1
    kill -USR1 "$$"
    printf 'trap=ignored\n'
    ;;
  path)
    who=$(id -un) || exit 1
    printf 'id=%s\n' "$who"
    ;;
  *)
    printf 'usage: %s ifs|cdpath|umask FILE|trap|path\n' "$0" >&2
    exit 64
    ;;
esac
