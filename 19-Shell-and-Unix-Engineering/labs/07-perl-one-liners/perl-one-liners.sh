#!/bin/sh
# Probes for the Perl One Liners note: each mode runs real perl one-liners and prints what perl did.
# Usage: perl-one-liners.sh MODE [WORKDIR]
# Modes that create files need WORKDIR, an existing scratch directory; they write only inside it
# and never remove it.
# shellcheck disable=SC2016 # single-quoted text is Perl code that perl expands, not the shell
# shellcheck disable=SC2312 # the tests assert each probe's printed values, not the status of the substitutions

mode=${1-}
dir=${2-}

need_dir() {
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    echo "usage: $0 $mode WORKDIR" >&2
    exit 2
  fi
  cd "$dir" || exit 2
}

# files: print the names in the current directory on one line, to prove no work file was left.
files() {
  set -- *
  printf '%s' "$*"
}

# lines_in FILE: print the number of lines perl reads from FILE.
lines_in() {
  perl -ne 'END { print $. + 0 }' "$1"
}

# inode FILE and mode_of FILE: stat fields, read through perl so no ls output is parsed.
inode() {
  perl -e 'print +(stat shift)[1]' "$1"
}
mode_of() {
  perl -e 'printf "%04o", (stat shift)[2] & 07777' "$1"
}

case $mode in
  version)
    # The perl that every other mode runs, as a dotted version and as the numeric $].
    perl -e 'printf "%vd %s\n", $^V, $]'
    ;;
  report)
    # The worked example: requests and bytes per HTTP status from a common-log-format file.
    printf '%s\n' \
      '10.0.0.1 - - [13/Nov/2026:10:00:00 +0000] "GET / HTTP/1.1" 200 512' \
      '10.0.0.2 - - [13/Nov/2026:10:00:01 +0000] "GET /a HTTP/1.1" 404 128' \
      '10.0.0.1 - - [13/Nov/2026:10:00:02 +0000] "GET /b HTTP/1.1" 200 2048' \
      '10.0.0.3 - - [13/Nov/2026:10:00:03 +0000] "GET /c HTTP/1.1" 304 -' \
      | perl -lane '$hits{$F[8]}++; $bytes{$F[8]} += $F[9] }{ print "$_ $hits{$_} $bytes{$_}" for sort keys %hits'
    ;;
  wrap)
    # -n and -p paste literal text around the program, so "}{" closes the loop early.
    printf '1\n2\n3\n' | perl -lne '$s += $_ }{ print "sum=$s"'
    printf 'x\n' | perl -pe 'BEGIN { print "begin\n" } END { print "end\n" }'
    # next still runs the -p continue block (the line prints); last skips it (the loop ends).
    printf 'a\nb\n' | perl -pe 'next if /a/; $_ = uc'
    printf 'a\nb\nc\n' | perl -pe 'last if /b/; $_ = "kept:$_"'
    printf 'b\na\nb\nc\na\n' | perl -ne 'print "uniq:$_" unless $seen{$_}++'
    ;;
  chomp)
    # The default split (' ') treats the newline as whitespace and drops it; a -F pattern keeps
    # it in the last field unless -l chomps first. -l chomps only $/, never a CR.
    printf 'a bc\n' | perl -ane 'print "default-no-l=", length $F[-1], "\n"'
    printf 'a:bc\n' | perl -F: -ane 'print "F-no-l=", length $F[-1], "\n"'
    printf 'a:bc\n' | perl -F: -lane 'print "F-with-l=", length $F[-1]'
    printf 'abc\r\n' | perl -lne 'print "crlf=", length'
    ;;
  autosplit)
    printf 'a:b::\n' | perl -F: -lane 'print "perl-F=", scalar @F'
    printf 'a:b::\n' | perl -lne 'print "split-1=", scalar(my @f = split /:/, $_, -1)'
    printf 'a:b::\n' | awk -F: '{ print "awk=" NF }'
    printf 'a|b\n' | perl -F'|' -lane 'print "F-pipe=", scalar @F, " second=", $F[1]'
    printf 'a|b\n' | perl -F'\|' -lane 'print "F-escaped=", scalar @F, " second=", $F[1]'
    printf 'a.b\n' | perl -F. -lane 'print "F-dot=", scalar @F'
    printf '  a  b \n' | perl -lane 'print "default=", scalar @F, " first=", $F[0]'
    # -F implies -a and -n (perl 5.20 and later): no -n or -a is given here.
    printf 'a:b\n' | perl -F: -le 'print "implied=", $F[1]'
    ;;
  ors-order)
    # -l copies $/ into $\ when the switch is parsed, so switch order decides the output separator.
    perl -ln0e 'BEGIN { printf "ln0: ors=%d rs=%d\n", ord $\, ord $/ }' </dev/null
    perl -0 -l -ne 'BEGIN { printf "0l: ors=%d rs=%d\n", ord $\, ord $/ }' </dev/null
    perl -00 -l -ne 'BEGIN { printf "00l: ors=%vd rs-empty=%d\n", $\, $/ eq "" }' </dev/null
    printf 'a\000b\000' | perl -ln0e 'print "record=<$_>"'
    ;;
  slurp)
    printf 'a\\\nb\nc\n' | perl -0777 -pe 's/\\\n//g'
    printf 'p1a\np1b\n\n\n\np2\n' | perl -00 -nle 'printf "para=%d\n", length'
    if out=$(printf 'a\nb\n' | perl -g -ne 'print length' 2>/dev/null); then
      echo "g=$out"
    else
      echo "g=rejected"
    fi
    ;;
  eof)
    need_dir
    printf 'a\nb\n' >f1
    printf 'c\n' >f2
    perl -ne 'print "cumulative $ARGV:$.\n" if eof' f1 f2
    perl -ne 'print "reset $ARGV:$.\n" if eof; close ARGV if eof' f1 f2
    perl -ne 'print "last-file-only $_" if eof()' f1 f2
    ;;
  inject)
    need_dir
    # Two-argument open: a trailing | runs a command, a leading > truncates a file.
    out=$(perl -ne print 'printf INJECTED |' 2>/dev/null)
    echo "diamond=[$out]"
    out=$(perl -e 'print while <<>>' 'printf INJECTED |' 2>/dev/null)
    echo "double-diamond=[$out]"
    out=$(perl -pi -e 1 'printf INJECTED |' 2>/dev/null)
    echo "inplace=[$out]"
    printf 'keep me\n' >victim
    perl -ne print '>victim' 2>/dev/null
    echo "victim-bytes=$(perl -e 'print -s shift || 0' victim)"
    printf 'keep me\n' >victim
    perl -e 'print while <<>>' '>victim' 2>/dev/null
    echo "victim-bytes-after-double-diamond=$(perl -e 'print -s shift || 0' victim)"
    ;;
  inplace-fail)
    need_dir
    printf 'a\nb\nc\n' >f
    perl -pi -e 'die "boom\n" if $. == 2' f 2>/dev/null
    st=$?
    echo "die status=$st lines=$(lines_in f) files=$(files)"
    printf 'a\nb\nc\n' >f
    perl -pi -e 'exit 3 if $. == 2' f
    st=$?
    echo "exit3 status=$st lines=$(lines_in f) files=$(files)"
    printf 'a\nb\nc\n' >f
    perl -pi -e 'exit if $. == 2' f
    st=$?
    echo "exit0 status=$st lines=$(lines_in f) files=$(files)"
    printf 'a\nb\nc\n' >f
    perl -pi -e 'last if $. == 2' f
    st=$?
    echo "last status=$st lines=$(lines_in f) files=$(files)"
    ;;
  inplace-links)
    need_dir
    printf 'old\n' >target
    ln -s target link
    perl -pi -e 's/old/new/' link
    if [ -L link ]; then kind=symlink; else kind=regular; fi
    echo "link=$kind link-content=$(cat link) target-content=$(cat target)"
    printf 'old\n' >h1
    ln h1 h2
    perl -pi -e 's/old/new/' h1
    echo "h1=$(cat h1) h2=$(cat h2)"
    printf 'old\n' >ro
    chmod 444 ro
    perl -pi -e 's/old/new/' ro
    st=$?
    echo "ro status=$st content=$(cat ro) mode=$(mode_of ro)"
    printf 'old\n' >bk
    before=$(inode bk)
    perl -pi.orig -e 's/old/new/' bk
    after=$(inode bk)
    backup=$(inode bk.orig)
    if [ "$after" = "$before" ]; then edited=same; else edited=new; fi
    if [ "$backup" = "$before" ]; then saved=original; else saved=copy; fi
    echo "bk=$(cat bk) bk.orig=$(cat bk.orig) edited-inode=$edited backup-inode=$saved"
    ;;
  inplace-dir)
    need_dir
    mkdir rodir
    printf 'old\n' >rodir/x
    chmod 555 rodir
    perl -pi -e 's/old/new/' rodir/x 2>err
    st=$?
    chmod 755 rodir
    case $(cat err) in
      *"Cannot make temp name"*) warned=yes ;;
      *) warned=no ;;
    esac
    echo "status=$st content=$(cat rodir/x) warned=$warned"
    ;;
  fatal-inplace)
    # The skip-and-continue warnings belong to the "inplace" category; FATAL turns them into a
    # nonzero exit, for a missing file under -n and an uneditable file under -i alike.
    need_dir
    perl -Mwarnings=FATAL,inplace -ne print nosuch 2>/dev/null
    st=$?
    echo "missing-file status=$st"
    mkdir rodir
    printf 'old\n' >rodir/x
    chmod 555 rodir
    perl -Mwarnings=FATAL,inplace -pi -e 's/old/new/' rodir/x 2>/dev/null
    st=$?
    chmod 755 rodir
    echo "read-only-dir status=$st content=$(cat rodir/x)"
    ;;
  exit-status)
    need_dir
    perl -ne print nosuch 2>/dev/null
    st=$?
    echo "missing-file status=$st"
    printf 'x\n' >small
    perl -e 'print "123456789\n" x 100000' >big
    # Standard output opened read-only: every write(2) to it fails with EBADF.
    perl -pe 1 small 2>err 1</dev/null
    st=$?
    echo "p-small status=$st error=$(head -n 1 err)"
    perl -pe 1 big 2>err 1</dev/null
    st=$?
    echo "p-big status=$st error=$(head -n 1 err)"
    perl -ne print big 2>err 1</dev/null
    st=$?
    echo "n-big status=$st error=$(head -n 1 err)"
    ;;
  unicode)
    e=$(printf '\303\251')
    printf '%s\n' "$e" | perl -nle 'print "default=", length'
    printf '%s\n' "$e" | perl -CS -nle 'print "CS=", length'
    printf '%s\n' "$e" | LC_ALL=C.UTF-8 perl -nle 'print "utf8-locale-no-C=", length'
    printf '%s\n' "$e" | LC_ALL=C.UTF-8 perl -CSL -nle 'print "CSL-utf8-locale=", length'
    printf '%s\n' "$e" | LC_ALL=C perl -CSL -nle 'print "CSL-C-locale=", length'
    printf '%s\n' "$e" | perl -nle 'print "uc-default=", uc eq $_ ? "unchanged" : "changed"'
    printf '%s\n' "$e" | perl -CS -nle 'print "uc-CS=", uc eq $_ ? "unchanged" : "changed"'
    ;;
  numbers)
    perl -le 'print "sum=", 0.1 + 0.2; print "equal=", (0.1 + 0.2 == 0.3 ? "yes" : "no")'
    perl -le 'print "big=", 1234567.891; print "pow=", 2**53 + 1; print "literal=", 9007199254740993'
    awk 'BEGIN { print "awk-big=" 1234567.891 }'
    ;;
  coerce)
    printf '5\n-\n7\n' | perl -lne '$s += $_ }{ print "quiet sum=$s"' 2>&1
    printf '5\n-\n7\n' | perl -wlne '$s += $_ }{ print "warned sum=$s"' 2>&1
    ;;
  regex-eval)
    # A pattern taken from a variable may not run code unless "use re 'eval'" is in effect.
    out=$(printf 'x\n' | PAT='(?{ $ran = 1 })' perl -ne 'print "matched\n" if /$ENV{PAT}/; END { print "ran=", ($ran ? 1 : 0), "\n" }' 2>&1)
    st=$?
    case $out in
      *"ran=1"*) result=executed ;;
      *"Eval-group not allowed at runtime"*) result=refused ;;
      *) result="other: $out" ;;
    esac
    echo "status=$st result=$result"
    ;;
  shell-inject)
    v='1; print "INJECTED"'
    # Shell interpolation into the program text: the value becomes Perl code.
    perl -le "print $v"
    # Data paths: the value stays data.
    V=$v perl -le 'print "env=$ENV{V}"'
    perl -le 'print "argv=$ARGV[0]"' "$v"
    perl -s -le 'print "s=$v"' -- -v="$v"
    ;;
  quoting)
    perl -le 'print "close-reopen=it'\''s"'
    perl -le 'print "hex-escape=it\x27s"'
    perl -le 'print q{q-braces=it'\''s}'
    ;;
  *)
    echo "usage: $0 MODE [WORKDIR]" >&2
    exit 2
    ;;
esac
