#!/bin/sh
# shellcheck disable=SC2016 # the code strings are for eval in the shell under test
# coproc-ksh.sh MODE: probes of the Korn shell co-process, cmd |& (ksh93 and mksh).
#   round   start cat |&, write with print -p, read with read -p
#   second  start a second co-process while the first still runs
#   move    move the first co-process to descriptors 3 and 4 with exec 3>&p 4<&p, then start another
#   eof     wc -l |&: the only way to send end of file is exec 3>&p; exec 3>&-
# The ksh syntax is a parse error in other shells, so every probe is an eval string; the code is
# also kept away from formatters that do not know the |& operator.

mode=${1:?usage: coproc-ksh.sh round|second|move|eof}

round='cat |&
print -p hi
read -p x
echo "round=$x"'

# A second |& while the first co-process runs is an error, which ends a non-interactive shell.
second='cat |&
cat |&
echo started'

move='cat |&
exec 3>&p 4<&p
cat |&
print -p two
read -p y
print -u3 three
read -u4 z
echo "new=$y old=$z"'

eof='wc -l |&
print -p a
print -p b
print -p c
exec 3>&p
exec 3>&-
read -p n
echo "count=$n"'

case $mode in
  round) eval "$round" ;;
  second) eval "$second" ;;
  move) eval "$move" ;;
  eof) eval "$eof" ;;
  *)
    echo "coproc-ksh.sh: unknown mode: $mode" >&2
    exit 2
    ;;
esac
