# shellcheck shell=sh
# Functions under test for the minispec specs; sourced, never executed.

greet() {
  printf 'hello %s\n' "$1"
}

bump() {
  COUNTER=$((${COUNTER:-0} + 1))
}

die3() {
  exit 3
}

noisy() {
  printf 'out\n'
  printf 'err\n' >&2
  return 2
}

trailing() {
  printf 'a\n\n\n'
}

stops_early() {
  false
  printf 'reached\n'
}

slurp() {
  cat
}
