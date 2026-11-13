#!/bin/sh
# dialects.sh: print which of the three coprocess syntaxes the running shell accepts.
#   bash  coproc NAME { cmd; } with the NAME[0] and NAME[1] descriptors
#   ksh   cmd |& with print -p and read -p (ksh88, ksh93, pdksh, mksh)
#   zsh   coproc cmd with print -p and read -p
# Each attempt runs in a subshell through eval, so a syntax the shell rejects cannot stop the
# script, and stdin is /dev/null, so a read that falls back to stdin returns at once.
# The coprocess is cat, which writes each chunk as soon as it reads it, so a working dialect
# answers "hi" and a broken one answers nothing.

try() {
  out=$( (eval "$1") </dev/null 2>/dev/null) || out=
  if [ "$out" = hi ]; then echo yes; else echo no; fi
}

# shellcheck disable=SC2016 # the code is for eval in the shell under test
bash_code='coproc C { cat; }
echo hi >&"${C[1]}"
read -r x <&"${C[0]}"
echo "$x"'
# shellcheck disable=SC2016 # the code is for eval in the shell under test
ksh_code='cat |&
print -p hi
read -p x
echo "$x"'
# shellcheck disable=SC2016 # the code is for eval in the shell under test
zsh_code='coproc cat
print -p hi
read -p x
echo "$x"'

b=$(try "$bash_code")
k=$(try "$ksh_code")
z=$(try "$zsh_code")
echo "bash=$b ksh=$k zsh=$z"
