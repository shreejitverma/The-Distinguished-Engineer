#!/bin/sh
# find-exec.sh DIR MODE: start sh -c from find -exec for every regular file under DIR.
#   embed  splice {} into the code string: find substitutes the name, sh parses it as code (unsafe)
#   argv   pass {} after the command_name "sh", so the name arrives as $1 (safe)
#   batch  the same with {} +, so one sh receives many names in "$@" (safe, fewer processes)
set -eu

dir=$1
case $2 in
  embed)
    # shellcheck disable=SC2156 # splicing {} into sh -c is the defect this mode demonstrates
    find "$dir" -type f -exec sh -c 'printf "<%s>\n" "{}"' \;
    ;;
  argv)
    # shellcheck disable=SC2016 # $1 belongs to the inner sh, not to this script
    find "$dir" -type f -exec sh -c 'printf "<%s>\n" "$1"' sh {} \;
    ;;
  batch)
    # shellcheck disable=SC2016 # "$@" and $# belong to the inner sh, not to this script
    find "$dir" -type f -exec sh -c 'printf "%s names:" "$#"; printf " <%s>" "$@"; printf "\n"' sh {} +
    ;;
  *)
    printf 'usage: %s DIR embed|argv|batch\n' "$0" >&2
    exit 64
    ;;
esac
