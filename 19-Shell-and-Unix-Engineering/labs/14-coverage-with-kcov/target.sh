#!/usr/bin/env bash
# target: a small script with a function, a case, a loop, a command substitution,
# a child shell, a here document, and a branch that only runs when FAIL=1.
classify() {
  case $1 in
    0) echo zero ;;
    [1-9]*) echo positive ;;
    *) echo other ;;
  esac
}

rc=0
if [ "$#" -eq 0 ]; then
  echo "no arguments"
else
  for a in "$@"; do
    classify "$a"
  done
fi
upper=$(echo "child" | tr '[:lower:]' '[:upper:]')
echo "$upper"
bash -c 'echo "from a child shell"'
cat <<EOF
heredoc line one
heredoc line two
EOF
if [ "${FAIL-}" = 1 ]; then
  echo "failing"
  rc=3
fi
exit "$rc"
