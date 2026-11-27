#!/bin/sh
# interleave.sh MODE ARGS...
#
# Drives concurrent writers from the shell under test and reports what landed in a file.
#   append N M FILE      N background writers, each line a fresh open(O_APPEND) via `>>`
#   shared N M FILE      N background writers sharing ONE open file description (`exec 3>FILE`, `>&3`)
#   separate N M FILE    N background writers, each with its own `<>` descriptor and its own offset
#   pipe N M FILE        N background writers into one pipe, drained by `cat >FILE`
#   holes append|plain FILE
#                        open fd 3 with `>>` (append) or `>` (plain), truncate FILE behind it, write again
#   merge dup|twoopen|twoappend FILE
#                        stdout line then stderr line into FILE via `>F 2>&1`, `>F 2>F`, or `>>F 2>>F`
#   big PROBE FILE
#                        4 processes of PROBE (the oiprobe binary), each appending 16 single 256 KiB writes
#                        with `>>`-style O_APPEND, then PROBE checks every 256 KiB block
# Every writer line is 100 bytes: 99 copies of the writer's letter and a newline.
# Output: one line of key=value pairs computed from FILE.
# shellcheck disable=SC2312 # a failed wc or tr would print a wrong number, and every test compares the number exactly
set -u

mode=$1
shift

# letter_line LETTER: print the 99-byte body of a writer line (no newline).
letter_line() {
  printf '%99s' '' | tr ' ' "$1"
}

# report FILE: bytes, newline count, and how many lines are 99 copies of one writer letter.
report() {
  bytes=$(wc -c <"$1" | tr -d ' ')
  lines=$(wc -l <"$1" | tr -d ' ')
  intact=$(awk 'length($0) == 99 && ($0 ~ /^A+$/ || $0 ~ /^B+$/ || $0 ~ /^C+$/ || $0 ~ /^D+$/) { n++ } END { print n + 0 }' "$1")
  printf 'bytes=%s lines=%s intact=%s\n' "$bytes" "$lines" "$intact"
}

# letter_of N: writer index 1..4 to its letter.
letter_of() {
  case $1 in
    1) echo A ;;
    2) echo B ;;
    3) echo C ;;
    4) echo D ;;
    *) echo Z ;;
  esac
}

# start_writers KIND N M FILE: run N background writers of KIND, then wait for all of them.
# Subshells inherit the group's stdout (pipe mode) and fd 3 (shared mode).
start_writers() {
  kind=$1
  n=$2
  m=$3
  file=$4
  w=1
  while [ "$w" -le "$n" ]; do
    letter=$(letter_of "$w")
    body=$(letter_line "$letter")
    (
      case $kind in
        append)
          i=0
          while [ "$i" -lt "$m" ]; do
            printf '%s\n' "$body" >>"$file"
            i=$((i + 1))
          done
          ;;
        shared)
          i=0
          while [ "$i" -lt "$m" ]; do
            printf '%s\n' "$body" >&3
            i=$((i + 1))
          done
          ;;
        separate)
          exec 4<>"$file"
          i=0
          while [ "$i" -lt "$m" ]; do
            printf '%s\n' "$body" >&4
            i=$((i + 1))
          done
          ;;
        pipe)
          i=0
          while [ "$i" -lt "$m" ]; do
            printf '%s\n' "$body"
            i=$((i + 1))
          done
          ;;
        *) exit 2 ;;
      esac
    ) &
    w=$((w + 1))
  done
  wait
}

case $mode in
  append)
    : >"$3"
    start_writers append "$1" "$2" "$3"
    report "$3"
    ;;
  shared)
    exec 3>"$3"
    start_writers shared "$1" "$2" "$3"
    exec 3>&-
    report "$3"
    ;;
  separate)
    : >"$3"
    start_writers separate "$1" "$2" "$3"
    report "$3"
    ;;
  pipe)
    : >"$3"
    {
      start_writers pipe "$1" "$2" /dev/null
    } | cat >"$3"
    report "$3"
    ;;
  holes)
    : >"$2"
    case $1 in
      append) exec 3>>"$2" ;;
      plain) exec 3>"$2" ;;
      *) exit 2 ;;
    esac
    printf 'aaaa\n' >&3
    : >"$2"
    printf 'b\n' >&3
    exec 3>&-
    printf 'bytes=%s\n' "$(wc -c <"$2" | tr -d ' ')"
    ;;
  merge)
    : >"$2"
    case $1 in
      dup) {
        printf 'stdout-line\n'
        printf 'e\n' >&2
      } >"$2" 2>&1 ;;
      twoopen) {
        printf 'stdout-line\n'
        printf 'e\n' >&2
      } >"$2" 2>"$2" ;;
      twoappend) {
        printf 'stdout-line\n'
        printf 'e\n' >&2
      } >>"$2" 2>>"$2" ;;
      *) exit 2 ;;
    esac
    printf 'bytes=%s content=%s\n' "$(wc -c <"$2" | tr -d ' ')" "$(tr '\n' '|' <"$2")"
    ;;
  big)
    : >"$2"
    for letter in A B C D; do
      "$1" appendw "$2" 16 262144 "$letter" &
    done
    wait
    "$1" blocks "$2" 262144
    ;;
  *)
    echo "interleave.sh: unknown mode $mode" >&2
    exit 2
    ;;
esac
