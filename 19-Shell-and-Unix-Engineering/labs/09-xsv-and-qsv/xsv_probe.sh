#!/bin/sh
# xsv_probe.sh - Demonstrates RFC 4180 parsing, index-assisted queries, and line-tool failure modes.
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
INDEXER="$SCRIPT_DIR/csv_indexer"

usage() {
  printf 'Usage: %s <mode> [args...]\n' "$0" >&2
  printf 'Modes: naive-count, naive-cut, naive-join, indexed-index, indexed-count, indexed-slice, validate-index, hash-join\n' >&2
  exit 1
}

[ "$#" -ge 1 ] || usage
mode="$1"
shift

case "$mode" in
  naive-count)
    [ "$#" -ge 1 ] || {
      printf 'Missing file argument\n' >&2
      exit 1
    }
    wc -l <"$1" | tr -d ' '
    ;;

  naive-cut)
    [ "$#" -ge 2 ] || {
      printf 'Usage: naive-cut <file> <fields>\n' >&2
      exit 1
    }
    cut -d, -f"$2" "$1"
    ;;

  naive-join)
    [ "$#" -ge 4 ] || {
      printf 'Usage: naive-join <file1> <file2> <field1> <field2>\n' >&2
      exit 1
    }
    join -t, -1 "$3" -2 "$4" "$1" "$2"
    ;;

  indexed-index)
    [ "$#" -ge 2 ] || {
      printf 'Usage: indexed-index <file.csv> <file.idx>\n' >&2
      exit 1
    }
    "$INDEXER" index "$1" "$2"
    ;;

  indexed-count)
    [ "$#" -ge 1 ] || {
      printf 'Usage: indexed-count <file.idx>\n' >&2
      exit 1
    }
    "$INDEXER" count "$1"
    ;;

  indexed-slice)
    [ "$#" -ge 4 ] || {
      printf 'Usage: indexed-slice <file.csv> <file.idx> <start> <length>\n' >&2
      exit 1
    }
    "$INDEXER" slice "$1" "$2" "$3" "$4"
    ;;

  validate-index)
    [ "$#" -ge 2 ] || {
      printf 'Usage: validate-index <file.csv> <file.idx>\n' >&2
      exit 1
    }
    "$INDEXER" validate "$1" "$2"
    ;;

  hash-join)
    [ "$#" -ge 4 ] || {
      printf 'Usage: hash-join <file1> <file2> <field1> <field2>\n' >&2
      exit 1
    }
    # Pure POSIX awk hash-join implementation (emulating xsv join without pre-sorting)
    awk -F, -v k1="$3" -v k2="$4" '
      NR == FNR {
        if (FNR == 1) {
          h1 = $0
          next
        }
        key = $k1
        rows[key] = $0
        next
      }
      FNR == 1 {
        printf "%s,%s\n", h1, $0
        next
      }
      {
        key = $k2
        if (key in rows) {
          printf "%s,%s\n", rows[key], $0
        }
      }
    ' "$1" "$2"
    ;;

  *)
    printf 'Unknown mode: %s\n' "$mode" >&2
    exit 1
    ;;
esac
