#!/usr/bin/env bash
# count-keys.sh: count how often each distinct input line occurs, using a bash associative array.
# Safe for every line, including an empty one, `*`, `@`, `]`, spaces, and `$(cmd)`; the lab proves it on bash 4.4 to 5.3.
# Usage: count-keys.sh [--drop LINE] <input
#   --drop LINE   remove LINE from the table before printing (exercises the safe `unset` form)
# Output: one "COUNT<TAB>LINE" per distinct line, by count descending, then by line in byte order.
# Exit status: 0 on success; 2 on a usage error or when the shell has no associative arrays.

# Plain POSIX syntax up to here, so any shell that runs this file reaches the check and stops.
case ${BASH_VERSION-} in
  [4-9].* | [1-9][0-9].*) ;;
  *)
    printf 'count-keys: needs bash 4.0 or later (this shell: %s)\n' "${BASH_VERSION:-not bash}" >&2
    exit 2
    ;;
esac

usage() {
  printf 'usage: count-keys.sh [--drop LINE] <input\n' >&2
  exit 2
}

have_drop=0
drop=
case $# in
  0) ;;
  2)
    [ "$1" = --drop ] || usage
    have_drop=1
    drop=$2
    ;;
  *) usage ;;
esac

declare -A count

# Every key gets a "k" prefix: bash rejects an empty key ("bad array subscript"),
# bash 4.4 rejects `[@]=` in a compound assignment, and bash 5.1 `unset 'a[@]'` drops the whole array.
while IFS= read -r line || [ -n "$line" ]; do
  key="k$line"
  # An assignment expands its subscript exactly once in every bash release.
  count[$key]=$((${count[$key]-0} + 1))
done

if [ "$have_drop" -eq 1 ]; then
  key="k$drop"
  # Single quotes: unset itself expands $key once, so the key's text is never evaluated.
  # A double-quoted "count[$key]" is expanded twice and runs any $(...) inside the key.
  unset 'count[$key]'
fi

tab=$'\t'
for key in "${!count[@]}"; do
  printf '%s\t%s\n' "${count[$key]}" "${key#k}"
done | LC_ALL=C sort -t "$tab" -k1,1nr -k2
