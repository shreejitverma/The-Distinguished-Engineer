# shellcheck shell=bash
# stack-lib.bash: sourced by stack.bash; h prints the call stack from three frames down.
# shellcheck disable=SC2312 # each substitution is caller or a string helper; the output shows failures
h() {
  echo "FUNCNAME=${FUNCNAME[*]}"
  echo "BASH_SOURCE=$(basenames "${BASH_SOURCE[@]}")"
  echo "BASH_LINENO=${BASH_LINENO[*]}"
  echo "caller=$(short "$(caller)")"
  echo "caller 0=$(short "$(caller 0)")"
  echo "caller 1=$(short "$(caller 1)")"
  echo "caller 2=$(short "$(caller 2)")"
  caller 3 >/dev/null || echo "caller 3 fails: status $?"
}
# basenames PATH...: print the last component of each PATH, separated by spaces.
basenames() {
  local p out=
  for p; do
    out="$out ${p##*/}"
  done
  echo "${out# }"
}
# short LINE: shorten the absolute file name that ends a caller or declare -F line.
short() {
  echo "${1% *} ${1##*/}"
}
