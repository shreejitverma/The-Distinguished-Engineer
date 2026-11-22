#!/bin/sh
# grep-probe: show how the grep on PATH behaves at its edges.
# Usage: grep-probe.sh MODE DIR
# MODE is one of: flavor exit patterns binary maxcount recurse selfout dashfile escape.
# DIR is an existing scratch directory the probe may fill; the probe never removes it.
# Every mode prints key=<value> lines that the bats file asserts per implementation.
# shellcheck disable=SC2312 # every report line prints computed values; a failed command shows up as a wrong value the tests catch
set -u

mode=${1:?usage: grep-probe.sh MODE DIR}
dir=${2:?usage: grep-probe.sh MODE DIR}
[ -d "$dir" ] || {
  echo "grep-probe: $dir is not a directory" >&2
  exit 2
}
cd "$dir" || exit 2
LC_ALL=C
export LC_ALL

# flavor: name the implementation behind the grep this shell runs.
flavor() {
  v=$(grep --version 2>&1 </dev/null)
  case $v in
    *"GNU grep"*) echo gnu ;;
    *"BSD grep"*) echo bsd ;;
    *BusyBox*) echo busybox ;;
    *) echo unknown ;;
  esac
}

# join: read lines on stdin and print them joined by commas (each followed by one).
join() {
  tr '\n' ,
}

case $mode in
  flavor)
    echo "flavor=$(flavor)"
    ;;

  exit)
    printf 'hit\n' >good.txt
    grep hit good.txt >/dev/null
    echo "match=$?"
    grep zzz good.txt >/dev/null
    echo "nomatch=$?"
    grep hit missing.txt good.txt >/dev/null 2>&1
    echo "missing_then_match=$?"
    grep -q hit missing.txt good.txt 2>/dev/null
    echo "q_missing_then_match=$?"
    grep -s hit missing.txt good.txt >/dev/null 2>s.err
    rc=$?
    echo "s_missing_then_match=$rc s_stderr_bytes=$(wc -c <s.err | tr -d ' ')"
    count=$(grep -c zzz good.txt)
    echo "count_zero=<$count> status=$?"
    ;;

  patterns)
    printf 'one\ntwo\nthree\n' >in.txt
    nl='
'
    echo "newline_list=$(grep -c -e "one${nl}three" in.txt)"
    echo "empty_pattern=$(grep -c -e '' in.txt)"
    printf 'zzz\n\nyyy\n' >pats.txt
    echo "file_with_blank_line=$(grep -c -f pats.txt in.txt)"
    out=$(grep -c -f /dev/null in.txt)
    echo "empty_pattern_file=<$out> status=$?"
    echo "dot_bre=$(printf 'a.c\nabc\n' | grep -c 'a.c') dot_fixed=$(printf 'a.c\nabc\n' | grep -c -F 'a.c')"
    ;;

  binary)
    # small.bin: a matching line, then a line holding one NUL byte.
    printf 'match\n\000\n' >small.bin
    # mk OFFSET FILE: "match", then 100-byte filler lines up to OFFSET, then a NUL line.
    mk() {
      {
        printf 'match\n'
        awk -v n="$1" 'BEGIN {
          line = sprintf("%099d", 0); gsub(/0/, "y", line)
          for (t = 0; t + 100 <= n; t += 100) print line
        }'
        printf '\000\n'
      } >"$2"
    }
    mk 40000 near.bin
    mk 200000 far.bin
    for f in small.bin near.bin far.bin; do
      grep match "$f" >o.txt 2>e.txt
      rc=$?
      echo "$f: out=<$(head -n 1 o.txt)> err=<$(head -n 1 e.txt)> rc=$rc"
    done
    grep -a match small.bin >o.txt 2>e.txt
    rc=$?
    echo "a_small: out=<$(head -n 1 o.txt)> err=<$(head -n 1 e.txt)> rc=$rc"
    # The NUL is the second-to-last byte of each file.
    echo "nul_offset: near=$(($(wc -c <near.bin) - 2)) far=$(($(wc -c <far.bin) - 2))"
    ;;

  maxcount)
    printf 'a\nx1\nb\nx2\nc\n' >m.txt
    {
      first=$(grep -m 1 x)
      rest=$(cat | join)
    } <m.txt
    echo "grep=<$first> rest=<$rest>"
    ;;

  recurse)
    mkdir -p tree/sub outside
    printf 'hit\n' >outside/f
    printf 'hit\n' >tree/sub/real
    ln -s ../../outside/f tree/sub/link
    ln -s ../outside tree/dirlink
    ln -s tree tlink
    echo "r_tree=<$(grep -r hit tree | sort | join)>"
    echo "r_cmdline_link=<$(grep -r hit tlink | sort | join)>"
    echo "R_tree=<$(grep -R hit tree | sort | join)>"
    ;;

  selfout)
    printf 'x\n' >s.txt
    # shellcheck disable=SC2094 # reading and appending to one file is the case under test
    grep x s.txt >>s.txt 2>e.txt
    rc=$?
    echo "rc=$rc lines=$(wc -l <s.txt | tr -d ' ') err=<$(head -n 1 e.txt)>"
    ;;

  dashfile)
    mkdir -p dash
    cd dash || exit 2
    printf 'secret\n' >data
    : >./-v
    # shellcheck disable=SC2035 # the bare glob is the bug this mode demonstrates
    out=$(grep secret *)
    echo "star=<$out> status=$?"
    out=$(grep secret -- *)
    echo "dashdash=<$out> status=$?"
    out=$(grep -e secret ./*)
    echo "dot_slash=<$out> status=$?"
    ;;

  escape)
    out=$(printf 'a1\nad\n' | grep '\d' 2>e.txt | join)
    echo "backslash_d=<$out> err=<$(head -n 1 e.txt)>"
    ;;

  *)
    echo "grep-probe: unknown mode $mode" >&2
    exit 2
    ;;
esac
