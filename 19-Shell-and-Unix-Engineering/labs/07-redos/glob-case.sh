#!/bin/sh
# glob-case.sh MODE N K CPU_SECONDS [SUFFIX]
# Builds a subject of N 'a' bytes followed by SUFFIX (default empty), then asks whether it matches the
# shell pattern made of K copies of '*a' followed by '*b' (K + 1 stars in all).
#   MODE multi : one 'case' with the whole multi-star pattern (the shell's own matcher)
#   MODE commit: the same question answered one star at a time with ${var#*a},
#                committing to the leftmost 'a' each time, then one '*b' test
# The shell runs under a soft CPU-time limit of CPU_SECONDS (core dumps off), so a
# matcher that backtracks across stars is killed by SIGXCPU instead of hanging.
# Prints "match" or "nomatch" when it finishes.

mode=$1 n=$2 k=$3 cpu=$4 suffix=${5-}

# posh 0.14.1 has no ulimit builtin: there the caller must set the CPU limit
# before starting the shell (the lab's test does it from bash).
if command -v ulimit >/dev/null 2>&1; then
  ulimit -c 0
  ulimit -S -t "$cpu" || exit 4
fi

s= i=0
while [ "$i" -lt "$n" ]; do
  s=${s}a
  i=$((i + 1))
done
s=$s$suffix

case $mode in
  multi)
    p= i=0
    while [ "$i" -lt "$k" ]; do
      p="$p*a"
      i=$((i + 1))
    done
    p="$p*b"
    # eval makes every shell treat $p as a pattern (native zsh would not glob an
    # unquoted expansion in a case pattern).
    eval "case \$s in $p) echo match ;; *) echo nomatch ;; esac"
    ;;
  commit)
    rest=$s i=0
    while [ "$i" -lt "$k" ]; do
      case $rest in
        *a*) rest=${rest#*a} ;;
        *)
          echo nomatch
          exit 0
          ;;
      esac
      i=$((i + 1))
    done
    case $rest in
      *b) echo match ;;
      *) echo nomatch ;;
    esac
    ;;
  *)
    echo "usage: glob-case.sh multi|commit N K CPU_SECONDS [SUFFIX]" >&2
    exit 2
    ;;
esac
