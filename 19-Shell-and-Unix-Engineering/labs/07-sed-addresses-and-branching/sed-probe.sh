#!/bin/sh
# sed-probe.sh CASE [SED [ARG...]]: run one sed address or branching case whose result
# differs between implementations, and print "out=<output lines joined by |> st=<exit status>".
# Every multi-line script is passed as separate -e options, which POSIX joins with newlines,
# so no case depends on how the shell under test treats a newline inside quotes.
if [ "$#" -eq 0 ]; then
  echo "usage: sed-probe.sh CASE [SED [ARG...]]" >&2
  exit 2
fi
case_name=$1
shift
if [ "$#" -eq 0 ]; then
  set -- sed
fi

posix_env=
case $case_name in
  skipped-start) # a numeric first address the script jumps over
    input=$(printf '%s\n' 1 2 3 4 5)
    set -- "$@" -n -e '2b' -e '2,4p'
    ;;
  jumped-end) # the GNU sed 4.1 NEWS example: a numeric second address the script jumps over
    input=$(printf '%s\n' 1 2 3 4 5 6 7 8 9 10 11 12)
    set -- "$@" -e '5,8b' -e '1,5d'
    ;;
  zero-range) # GNU 0,/re/: the end regex may match line 1
    input=$(printf '%s\n' x a x)
    set -- "$@" -n -e '0,/x/p'
    ;;
  zero-alone) # line address 0 outside 0,/re/
    input=$(printf '%s\n' 1 2)
    set -- "$@" -n -e '0p'
    ;;
  semicolon-label) # does a label end at ";"?
    input=$(printf '%s\n' a b c)
    set -- "$@" -e ':a;N;$!ba;s/\n/+/g'
    ;;
  brace-close) # "}" directly after a command, with no ";" or newline
    input=$(printf '%s\n' 1 2)
    set -- "$@" -n -e '1{p}'
    ;;
  double-bang) # two "!" before one command
    input=$(printf '%s\n' 1 2 3)
    set -- "$@" -n -e '2!!p'
    ;;
  t-after-N) # does N, which reads an input line, clear the t flag?
    input=$(printf '%s\n' a b)
    set -- "$@" -e 's/a/X/' -e 'N' -e 't done' -e 's/$/ NO/' -e ':done'
    ;;
  n-eof) # N with no next line: print the pattern space or not?
    input=$(printf '%s\n' a b c)
    set -- "$@" -e 'N' -e 's/\n/+/'
    ;;
  n-eof-posix) # the same with POSIXLY_CORRECT in the environment
    input=$(printf '%s\n' a b c)
    set -- "$@" -e 'N' -e 's/\n/+/'
    posix_env=1
    ;;
  dup-label) # two ":x" labels: which one does "b x" reach?
    input=a
    set -- "$@" -n -e 'b x' -e ':x' -e 's/^/1/' -e ':x' -e 's/^/2/' -e 'p'
    ;;
  unreached-label) # a branch to a missing label that never executes
    input=a
    set -- "$@" -e '/zzz/b nowhere'
    ;;
  step) # GNU first~step
    input=$(printf '%s\n' 1 2 3 4 5)
    set -- "$@" -n -e '1~2p'
    ;;
  plus) # addr1,+N
    input=$(printf '%s\n' 1 2 3 4 5)
    set -- "$@" -n -e '2,+1p'
    ;;
  tilde) # GNU addr1,~N
    input=$(printf '%s\n' 1 2 3 4 5 6)
    set -- "$@" -n -e '2,~4p'
    ;;
  T) # GNU T: branch when no substitution succeeded
    input=$(printf '%s\n' a b)
    set -- "$@" -e 's/a/X/' -e 'T q' -e 's/$/!/' -e ':q'
    ;;
  c-split-e) # "c\" in one -e option and its text in the next (POSIX joins -e options with a newline)
    input=$(printf '%s\n' 1 2 3 4)
    set -- "$@" -e '2,3c\' -e 'X'
    ;;
  address-2048 | address-2049) # a script line of exactly 2048 or 2049 bytes: "/" RE "/=" with an RE of 2045 or 2046 bytes
    case $case_name in
      address-2048) re_len=2045 ;;
      *) re_len=2046 ;;
    esac
    input=$(awk -v n="$re_len" 'BEGIN { while (i++ < n) printf "a"; print "" }')
    set -- "$@" -n -e "/$input/="
    ;;
  icase) # the I flag on a context address
    input=Foo
    set -- "$@" -n -e '/foo/Ip'
    ;;
  *)
    echo "sed-probe.sh: unknown case: $case_name" >&2
    exit 2
    ;;
esac

if [ -n "$posix_env" ]; then
  out=$(printf '%s\n' "$input" | POSIXLY_CORRECT=1 "$@" 2>/dev/null)
  st=$?
else
  out=$(printf '%s\n' "$input" | "$@" 2>/dev/null)
  st=$?
fi
flat=$(printf '%s\n' "$out" | awk '{ printf "%s%s", sep, $0; sep = "|" } END { print "" }')
printf 'out=%s st=%s\n' "$flat" "$st"
