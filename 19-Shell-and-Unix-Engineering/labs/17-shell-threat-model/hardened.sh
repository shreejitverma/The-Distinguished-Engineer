#!/bin/sh
# hardened.sh: the probes of ambient.sh behind a prologue that resets the state a script can reset.
# It cannot undo code that ran before its first line (BASH_ENV, imported functions, .zshenv),
# and it cannot trap a signal ignored on entry; those need a clean boundary in the caller.
# Usage: hardened.sh FILE   (run from a directory holding work/)
set -u

# Prologue: reset every inherited input this script depends on.
umask 077
IFS=$(printf ' \t\nX')
IFS=${IFS%X}
PATH=/usr/bin:/bin
export PATH
unset CDPATH

file=${1:?usage: hardened.sh FILE}

v='a:b'
# shellcheck disable=SC2086,SC2248 # field splitting is the behavior under test
set -- $v
printf 'fields=%s\n' "$#"

out=$(cd ./work) || exit 1
cd ./work || exit 1
parent=$(cd .. && pwd -P) || exit 1
if [ -n "$out" ]; then printed=yes; else printed=no; fi
printf 'landed=%s printed=%s\n' "${parent##*/}" "$printed"

printf 'secret\n' >"$file"
perms=$(ls -ln "$file") || exit 1
printf 'mode=%s\n' "${perms%%[ +.@]*}"

who=$(id -un) || exit 1
printf 'id=%s\n' "$who"

# `command` skips shell functions, so an imported function cannot shadow the utility.
base=$(command basename /x/y) || exit 1
printf 'basename=%s\n' "$base"
