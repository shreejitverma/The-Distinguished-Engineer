#!/bin/sh
# tcp-probe: show how the tr, cut, and paste on PATH behave at their edges.
# Usage: tcp-probe.sh MODE DIR
# MODE is one of: flavor trpad trrange trclass trmb cutlist cutdelim cutmb
#   cutbad cuteol pastelayout pasteedge pastebad pastefd longline.
# DIR is an existing scratch directory the probe may fill; the probe never removes it.
# Every mode prints NAME: out=<bytes> rc=N err=<first stderr line> lines, where
# bytes shows printable ASCII as itself and every other byte as \n, \t, or \ooo.
set -u

mode=${1:?usage: tcp-probe.sh MODE DIR}
dir=${2:?usage: tcp-probe.sh MODE DIR}
[ -d "$dir" ] || {
  echo "tcp-probe: $dir is not a directory" >&2
  exit 2
}
cd "$dir" || exit 2
LC_ALL=C
export LC_ALL
# The UTF-8 locale every matrix image and macOS provide.
U8=C.UTF-8

# vis: print stdin on one line, escaping every byte that is not printable ASCII.
vis() {
  od -An -v -t o1 | awk '
    {
      for (i = 1; i <= NF; i++) {
        n = 0
        for (j = 1; j <= length($i); j++) n = n * 8 + substr($i, j, 1)
        if (n == 10) s = s "\\n"
        else if (n == 9) s = s "\\t"
        else if (n >= 32 && n < 127 && n != 92) s = s sprintf("%c", n)
        else s = s sprintf("\\%03o", n)
      }
    }
    END { printf "%s\n", s }'
}

# run NAME INPUT CMD [ARG...]: run CMD with stdin from INPUT, then report its
# stdout bytes, exit status, and first line of stderr.
run() {
  name=$1
  input=$2
  shift 2
  "$@" <"$input" >"$name.out" 2>"$name.err"
  rc=$?
  out=$(vis <"$name.out")
  err=$(sed -n 1p "$name.err")
  printf '%s: out=<%s> rc=%s err=<%s>\n' "$name" "$out" "$rc" "$err"
}

# flavor_of UTIL: name the implementation behind UTIL as gnu, bsd, busybox, uutils, or unknown.
flavor_of() {
  v=$("$1" --version 2>&1 </dev/null)
  case $v in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"usage: $1 "*) echo bsd ;;
    *)
      path=$(command -v "$1") || path=$1
      real=$(readlink -f "$path" 2>/dev/null) || real=$path
      case ${real##*/} in
        busybox) echo busybox ;;
        *) echo unknown ;;
      esac
      ;;
  esac
}

# gnu_cut_mb: print yes if cut is GNU coreutils 9.11 or later (multibyte -c and -n), else no.
gnu_cut_mb() {
  line=$(cut --version 2>/dev/null | sed -n 1p)
  ver=${line##* }
  echo "$ver" | awk -F. '{ print ($1 > 9 || ($1 == 9 && $2 >= 11)) ? "yes" : "no" }'
}

: >empty
printf '1\n2\n3\n' >three
printf 'x\n' >one

case $mode in
  flavor)
    for u in tr cut paste; do
      f=$(flavor_of "$u")
      echo "$u=$f"
    done
    f=$(flavor_of cut)
    mb=n/a
    if [ "$f" = gnu ]; then
      mb=$(gnu_cut_mb)
    fi
    echo "cut_mb=$mb"
    ;;

  trpad)
    printf '0123456789\n' >digits
    run pad_short digits tr 0-9 ab
    run star_fill digits tr 0-9 '[d*]'
    run star_count digits tr 0-9 'a[b*3]c'
    run star_octal digits tr 0-9 '[b*010]c'
    printf 'a\000b\n' >nul
    run delete_nul nul tr -d '\000'
    ;;

  trrange)
    printf 'azm-\n' >azm
    run reversed azm tr z-a X
    printf 'a-b\n' >adb
    run dash_first adb tr -- -a X_
    run dash_last adb tr a- X_
    printf '[ab]\n' >brk
    run sysv_brackets brk tr '[a-b]' '[A-B]'
    ;;

  trclass)
    printf 'hello,  world 42!\n' >words
    run words_posix words tr -cs '[:alpha:]' '[\n*]'
    run words_short words tr -cs '[:alpha:]' '\n'
    printf 'abc\n' >abc
    run digit_in_s2 abc tr abc '[:digit:]'
    printf 'a b\n' >ab
    run delete_graph ab tr -d '[:graph:]'
    ;;

  trmb)
    # e-acute is the two bytes 303 251 in UTF-8.
    printf '\303\251a\n' >ea
    run upper_utf8 ea env LC_ALL="$U8" tr '[:lower:]' '[:upper:]'
    printf 'a\303\251b\n' >aeb
    run octal_pair aeb env LC_ALL="$U8" tr '\303\251' xy
    printf 'a\377b\n' >bad
    run invalid_utf8 bad env LC_ALL="$U8" tr a x
    run invalid_c bad env LC_ALL=C tr a x
    ;;

  cutlist)
    printf 'abcdefghi\n' >alpha
    run order alpha cut -c6,2,4-7,1
    printf 'a:b:c\n' >abc
    run field_order abc cut -d: -f3,1
    printf 'nodelim\na:b\n' >mixed
    run passthrough mixed cut -d: -f2
    run suppress mixed cut -s -d: -f2
    printf 'a:b\n' >ab
    run beyond ab cut -d: -f5
    run zero ab cut -d: -f0
    ;;

  cutdelim)
    printf 'a\000b\tc\n' >nulsep
    run empty_delim nulsep cut -d '' -f2
    printf 'a;b:c\n' >semi
    run two_char_delim semi cut -d ':;' -f1
    printf 'a  b\n' >spaces
    run space_f2 spaces cut -d ' ' -f2
    run space_f3 spaces cut -d ' ' -f3
    ;;

  cutmb)
    printf '\303\251tude\n' >etude
    run c1 etude env LC_ALL="$U8" cut -c1
    run b1 etude env LC_ALL="$U8" cut -b1
    run nb1 etude env LC_ALL="$U8" cut -n -b1
    run nb2 etude env LC_ALL="$U8" cut -n -b2
    ;;

  cutbad)
    printf 'x:1\n\377:2\nz:3\n' >bad
    run file_operand empty env LC_ALL="$U8" cut -d: -f2 bad
    run stdin_input bad env LC_ALL="$U8" cut -d: -f2
    run c_locale empty env LC_ALL=C cut -d: -f2 bad
    ;;

  cuteol)
    printf 'abc' >abc
    run past_end abc cut -c5
    printf 'a:b' >ab
    run field_no_newline ab cut -d: -f2
    ;;

  pastelayout)
    run long_first empty paste three one
    run long_last empty paste one three
    printf 'a\nb\nc\nd\ne\n' >five
    run stdin_twice five paste - -
    run cycle empty paste -d ',;' three three three
    run serial_cycle empty paste -s -d ',;' three
    run serial_nodelim empty paste -s -d '\0' three
    ;;

  pasteedge)
    run serial_empty empty paste -s empty
    printf 'a\nb' >noeol
    run serial_noeol empty paste -s noeol
    run empty_list empty paste -d '' one one
    run no_operand three paste
    ;;

  pastebad)
    printf 'a\377b\nc\n' >bad
    printf '1\n2\n' >nums
    run parallel empty env LC_ALL="$U8" paste bad nums
    run serial empty env LC_ALL="$U8" paste -s bad
    ;;

  pastefd)
    i=1
    while [ "$i" -le 40 ]; do
      printf 'line%s\n' "$i" >"f$i"
      i=$((i + 1))
    done
    # Parallel paste holds every operand open at once; serial paste opens one at a time.
    (
      # shellcheck disable=SC3045 # ulimit -n is XSI in POSIX.1-2024, and every matrix shell has it
      ulimit -n 20 || exit 99
      paste f* >parallel.out 2>parallel.err
      rc=$?
      n=$(wc -c <parallel.out)
      echo "parallel: rc=$rc stdout_bytes=$((n))"
      paste -s f* >serial.out 2>serial.err
      rc=$?
      n=$(wc -l <serial.out)
      echo "serial: rc=$rc lines=$((n))"
    )
    # The subshell's own status is 99 only when ulimit failed.
    echo "subshell=$?"
    ;;

  longline)
    # One line of 1 MiB plus ":z": far beyond LINE_MAX (2048 at minimum).
    head -c 1048576 </dev/zero | tr '\0' a >big
    printf ':z\n' >>big
    run field2 big cut -d: -f2
    cut -d: -f1 <big >f1.out
    n=$(wc -c <f1.out)
    echo "field1_bytes=$((n))"
    paste -d, big big >pasted
    n=$(wc -c <pasted)
    echo "pasted_bytes=$((n))"
    run tr_delete big tr -d a
    ;;

  *)
    echo "tcp-probe: unknown mode $mode" >&2
    exit 2
    ;;
esac
