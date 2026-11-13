#!/bin/sh
# coproc-zsh.sh MODE: probes of the zsh coproc reserved word.
#   round    coproc cat, then print -p and read -p, and an external command writing through >&p
#   replace  a second coproc closes the shell's descriptors to the first; a copy made with
#            exec 3>&p 4<&p keeps the first one reachable
#   eof      wc -l as a coprocess: save its output with exec 4<&p, then "coproc exit" closes the
#            shell's write end, so wc sees end of file and its count arrives on descriptor 4
# The zsh code runs through eval so that other shells and formatters never parse it.

mode=${1:?usage: coproc-zsh.sh round|replace|eof}

# shellcheck disable=SC2016 # the code is for eval in the shell under test
round='coproc cat
print -p hi
read -p x
/bin/sh -c "echo external" >&p
read -p y
echo "round=$x external=$y"'

# shellcheck disable=SC2016 # the code is for eval in the shell under test
replace='coproc cat
exec 3>&p 4<&p
coproc cat
print -p two
read -p y
print -u3 three
read -u4 z
echo "new=$y old=$z"'

# shellcheck disable=SC2016 # the code is for eval in the shell under test
eof='coproc wc -l
exec 4<&p
print -p a
print -p b
print -p c
coproc exit
read -r n <&4
echo "count=$n"'

case $mode in
  round) eval "$round" ;;
  replace) eval "$replace" ;;
  eof) eval "$eof" ;;
  *)
    echo "coproc-zsh.sh: unknown mode: $mode" >&2
    exit 2
    ;;
esac
