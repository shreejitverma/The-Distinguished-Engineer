#!/usr/bin/env bash
# guarded-assoc.sh WORD...: count each distinct WORD with an associative array, in first-seen
# order, and refuse to run on anything older than bash 4.0.
# The guard uses only syntax that bash 3.2 and every POSIX shell parse, so an old bash stops
# here with a clear message instead of silently treating every key as arithmetic index 0.
# The body is an eval string only so the lab matrix can run this file under non-bash shells,
# some of which (mksh) parse a whole script before running it.
case ${BASH_VERSION-} in
  '' | [0-3].*)
    printf 'guarded-assoc.sh: needs bash >= 4.0, running %s\n' "${BASH_VERSION:-a non-bash shell}" >&2
    exit 2
    ;;
  *) ;;
esac

eval '
declare -A count
order=()
for w in "$@"; do
  if [ -z "${count[$w]+set}" ]; then
    order+=("$w")
  fi
  count[$w]=$(( ${count[$w]:-0} + 1 ))
done
for w in ${order[@]+"${order[@]}"}; do
  printf "%s=%s\n" "$w" "${count[$w]}"
done
'
