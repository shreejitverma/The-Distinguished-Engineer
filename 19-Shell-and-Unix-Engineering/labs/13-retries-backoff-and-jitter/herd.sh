#!/bin/sh
# herd.sh: start N background subshells from one shell, as a parallel retry loop would, let each draw
# one jitter value from SOURCE, and print how many distinct values the herd drew.
# Usage: herd.sh SOURCE N
#   SOURCE urandom: 32 bits from /dev/urandom through od (one independent draw per worker)
#   SOURCE awk:     awk 'BEGIN { srand(); ... rand() }' (srand() with no argument seeds from the clock)
#   SOURCE random:  the shell's own $RANDOM, referenced only inside each worker
# Output: "distinct=K of N", or "RANDOM is unset" (exit 3) in a shell that has no $RANDOM.
# Scratch files live in a mktemp -d directory under TMPDIR, removed on exit.
src=$1 n=$2

draw() {
  # shellcheck disable=SC3028 # RANDOM is not POSIX; the point is to see what each shell does
  case $src in
    urandom) od -An -N4 -tu4 /dev/urandom | tr -d ' \n' ;;
    awk) awk 'BEGIN { srand(); printf "%d", rand() * 4294967296 }' ;;
    random) printf '%s' "${RANDOM-unset}" ;;
    *) return 2 ;;
  esac
  echo
}

case $src in
  urandom | awk | random) ;;
  *)
    echo "usage: herd.sh urandom|awk|random N" >&2
    exit 2
    ;;
esac
dir=$(mktemp -d "${TMPDIR:-/tmp}/herd.XXXXXX") || exit 1
trap 'rm -f "$dir"/w.*; rmdir "$dir"' EXIT

i=1
while [ "$i" -le "$n" ]; do
  draw >"$dir/w.$i" &
  i=$((i + 1))
done
wait

if grep -q unset "$dir"/w.*; then
  echo "RANDOM is unset"
  exit 3
fi
distinct=$(cat "$dir"/w.* | sort -u | wc -l)
printf 'distinct=%d of %d\n' "$distinct" "$n"
