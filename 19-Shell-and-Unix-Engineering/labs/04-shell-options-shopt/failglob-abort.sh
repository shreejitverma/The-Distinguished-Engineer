#!/usr/bin/env bash
# failglob-abort.sh - show how far a failglob error unwinds: the whole top-level command.
# Usage: failglob-abort.sh DIR   (DIR must hold no file named *.none)
# A shell with no shopt builtin fails on line "shopt -s failglob" and then runs everything,
# which is what happens when a bash script is started with sh.

cd "$1" || exit 1
shopt -s failglob

for pass in 1 2; do
  echo "pass $pass"
  echo ./*.none
  echo "pass $pass done"
done
echo "after loop status=$?"

report() {
  echo in-function
  echo ./*.none
  echo after-glob-in-function
}
report
echo "after function status=$?"

captured=$(
  echo ./*.none
  echo inside-substitution
)
echo "after substitution status=$? captured=[$captured]"
