#!/bin/sh
# sort-probe.sh MODE SORTCMD: run the sort(1) experiments of MODE with SORTCMD, one string
# such as "sort", "gsort", or "busybox sort", and print one result line per experiment.
# Modes: ties, bounds, options, numeric, unique, check, twopass.
# Every experiment runs in the C locale, so text keys compare as bytes.

LC_ALL=C
export LC_ALL

mode=$1
sort_cmd=$2

# run_sort [OPTION...]: run SORTCMD with the options; eval splits SORTCMD in every shell,
# including native zsh, which does not field split an unquoted expansion.
run_sort() {
  eval "$sort_cmd \"\$@\""
}

# report LABEL TEXT: print "LABEL: " and the lines of TEXT joined by "|".
report() {
  joined=$(printf '%s\n' "$2" | tr '\n' '|')
  printf '%s: %s\n' "$1" "${joined%|}"
}

# try LABEL INPUT [OPTION...]: sort INPUT (a printf %b string) and print
# "LABEL: LINE|LINE|...|status=N".
try() {
  label=$1
  input=$2
  shift 2
  result=$(
    printf '%b' "$input" | run_sort "$@" 2>/dev/null
    printf 'status=%s\n' "$?"
  )
  report "$label" "$result"
}

# check LABEL INPUT [OPTION...]: run a sort that is expected to print nothing on stdout
# (-c, -C, or a bad key) and print "LABEL: status=N stderr=yes|no".
check() {
  label=$1
  input=$2
  shift 2
  err=$(printf '%b' "$input" | run_sort "$@" 2>&1 >/dev/null)
  status=$?
  if [ -n "$err" ]; then said=yes; else said=no; fi
  printf '%s: status=%s stderr=%s\n' "$label" "$status" "$said"
}

case $mode in
  ties)
    try tie 'b 2\na 1\nb 1\na 2\n' -k1,1
    try stable 'b 2\na 1\nb 1\na 2\n' -s -k1,1
    try reverse 'b 2\na 1\nb 1\na 2\n' -r -k1,1
    try stable-reverse 'b 2\na 1\nb 1\na 2\n' -s -r -k1,1
    try fold 'a\nA\nb\nB\n' -f
    try fold-stable 'a\nA\nb\nB\n' -f -s
    ;;
  bounds)
    try open 'x 1 z\ny 1 a\n' -s -k2
    try closed 'x 1 z\ny 1 a\n' -s -k2,2
    try blanks 'a  9\nb 1\n' -k2,2
    try blanks-b 'a  9\nb 1\n' -b -k2,2
    try blanks-kb 'a  9\nb 1\n' -k2b,2
    try sep-default 'a  c\na b\n' -s -k3,3
    try sep-space 'a  c\na b\n' -s -t ' ' -k3,3
    try char 'p:zb\nq:za\n' -s -t : -k2.2,2.2
    ;;
  options)
    try inherit 'a 10\nb 9\nc 100\n' -n -k2,2
    try override 'a 10\nb 9\nc 100\n' -n -k2,2r
    ;;
  numeric)
    try n-exp '1e3\n2\n' -n
    try n-plus '+5\n3\n' -n
    try n-zero '-0\nb\n0\na\n' -n -s
    try g-exp '1e3\n2\n' -g
    ;;
  unique)
    try u-key 'b 1\na 2\nb 0\n' -u -k1,1
    try u-key-s 'b 1\na 2\nb 0\n' -s -u -k1,1
    try u-num '1\n01\n1.0\n' -n -u
    ;;
  check)
    check c-disorder 'b\na\n' -c
    check c-sorted 'a\nb\n' -c
    check C-disorder 'b\na\n' -C
    check cu-dup 'a\na\n' -c -u
    check k-zero 'a\n' -k0
    ;;
  twopass)
    input='a 10\nb 2\na 9\nb 10\n'
    try one-pass "$input" -k1,1 -k2,2n
    result=$(printf '%b' "$input" | run_sort -k2,2n | run_sort -s -k1,1)
    report two-pass-s "$result"
    result=$(printf '%b' "$input" | run_sort -k2,2n | run_sort -k1,1)
    report two-pass "$result"
    ;;
  *)
    printf 'usage: %s ties|bounds|options|numeric|unique|check|twopass SORTCMD\n' "$0" >&2
    exit 2
    ;;
esac
