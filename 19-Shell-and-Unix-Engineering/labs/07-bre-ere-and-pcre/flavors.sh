#!/bin/sh
# flavors.sh: feed the same patterns to grep as a BRE, an ERE, and a PCRE (-P),
# and to sed and expr, and print what each one selected.
# Usage: flavors.sh DIR   (DIR is an existing scratch directory for stdout and stderr captures)
# Each result line is: LABEL: out=<selected lines joined by commas, or none> st=<exit status> warn=<yes|no>
# warn=yes means the command wrote something on stderr (a warning, an error, or a usage message).
set -u
dir=${1:?usage: flavors.sh DIR}
LC_ALL=C
export LC_ALL

# join_lines: read lines on stdin and print them joined by commas, or "none".
join_lines() {
  joined=
  while IFS= read -r line; do
    joined=${joined:+$joined,}$line
  done
  printf '%s\n' "${joined:-none}"
}

# probe LABEL INPUT CMD [ARG...]: run CMD with INPUT (lines) on stdin and report the result.
probe() {
  label=$1
  input=$2
  shift 2
  printf '%s\n' "$input" | "$@" >"$dir/out" 2>"$dir/err"
  st=$?
  out=$(join_lines <"$dir/out")
  if [ -s "$dir/err" ]; then warn=yes; else warn=no; fi
  printf '%s: out=%s st=%s warn=%s\n' "$label" "$out" "$st" "$warn"
}

ops='a
aa
a+
(ab)
ab
a{2}
a|b
b'

echo '== 1. the same characters as BRE and ERE (grep -x over 8 lines)'
for re in 'a+' '(ab)' 'a{2}' 'a|b'; do
  probe "BRE $re" "$ops" grep -x -e "$re"
  probe "ERE $re" "$ops" grep -E -x -e "$re"
done
probe 'BRE a\+' "$ops" grep -x -e 'a\+'
probe 'BRE a\|b' "$ops" grep -x -e 'a\|b'

echo '== 2. anchors and a leading star'
probe 'BRE a^b' 'a^b' grep -e 'a^b'
probe 'ERE a^b' 'a^b' grep -E -e 'a^b'
probe 'BRE *a' '*a' grep -e '*a'
probe 'ERE *a' '*a' grep -E -e '*a'

echo '== 3. back-references'
probe 'BRE \(ab\)\1' 'abab
ab1' grep -x -e '\(ab\)\1'
probe 'ERE (ab)\1' 'abab
ab1' grep -E -x -e '(ab)\1'

echo '== 4. backslash escapes and brackets'
probe 'BRE \d' '5
d' grep -x -e '\d'
probe 'BRE [\n]' 'n
\
x' grep -x -e '[\n]'
probe 'PCRE [\n]' 'n
\
x' grep -P -x -e '[\n]'
probe 'PCRE \d' '5
d' grep -P -x -e '\d'

echo '== 5. which alternative wins'
probe 'sed -E s/a|ab/' 'ab' sed -E 's/a|ab/[&]/'
probe 'PCRE -o a|ab' 'ab' grep -P -o -e 'a|ab'

echo '== 6. the POSIX.1-2024 minimal repetition .*?'
probe 'sed -E s/.*?c/' 'abc abc' sed -E 's/.*?c/[&]/'
probe 'ERE -o .*?c' 'abc abc' grep -E -o -e '.*?c'
probe 'PCRE -o .*?c' 'abc abc' grep -P -o -e '.*?c'

echo '== 7. expr matches an anchored BRE'
probe 'expr a+b : a+' '' expr 'a+b' : 'a+'
probe 'expr xab : ab' '' expr 'xab' : 'ab'

echo '== 8. GNU sed --posix drops the BRE extensions'
probe 'sed s/a\+/' 'aa a+' sed 's/a\+/[&]/'
probe 'sed --posix s/a\+/' 'aa a+' sed --posix 's/a\+/[&]/'
