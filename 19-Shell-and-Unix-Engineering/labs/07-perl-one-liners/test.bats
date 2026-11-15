#!/usr/bin/env bats
# Tests for perl-one-liners.sh: what perl's command-line switches, the <> operator, and -i really do.
# The shell under test only quotes and launches perl, so every expected value below is perl's own
# behavior and is the same in every shell family; version-dependent results branch on perl's $].

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME/perl-one-liners.sh"
}

# perl_at_least V: succeed when the perl on PATH has $] >= V (for example 5.036).
perl_at_least() {
  perl -e 'exit($] >= $ARGV[0] ? 0 : 1)' "$1"
}

@test "perl is installed and is 5.28 or later, which every in-place test assumes" {
  run_lab "$LAB" version
  [ "$status" -eq 0 ]
  case $output in
    5.*) ;;
    *) false ;;
  esac
  perl_at_least 5.028
}

@test "the worked example aggregates hits and bytes per status, a '-' byte count adding 0" {
  run_lab "$LAB" report
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 3 ]
  [ "${lines[0]}" = "200 2 2560" ]
  [ "${lines[1]}" = "304 1 0" ]
  [ "${lines[2]}" = "404 1 128" ]
}

@test "-n and -p wrap the program text, so }{ ends the loop and next still prints under -p" {
  run_lab "$LAB" wrap
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "sum=6" ]
  [ "${lines[1]}" = "begin" ]
  [ "${lines[2]}" = "x" ]
  [ "${lines[3]}" = "end" ]
  [ "${lines[4]}" = "a" ]
  [ "${lines[5]}" = "B" ]
  [ "${lines[6]}" = "kept:a" ]
  [ "${lines[7]}" = "uniq:b" ]
  [ "${lines[8]}" = "uniq:a" ]
  [ "${lines[9]}" = "uniq:c" ]
  [ "${#lines[@]}" -eq 10 ]
}

@test "a -F field keeps the newline without -l, the default split drops it, and -l leaves a CR" {
  run_lab "$LAB" chomp
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "default-no-l=2" ]
  [ "${lines[1]}" = "F-no-l=3" ]
  [ "${lines[2]}" = "F-with-l=2" ]
  [ "${lines[3]}" = "crlf=4" ]
}

@test "-F drops trailing empty fields where awk keeps them, and -F'|' splits every character" {
  run_lab "$LAB" autosplit
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "perl-F=2" ]
  [ "${lines[1]}" = "split-1=4" ]
  [ "${lines[2]}" = "awk=4" ]
  [ "${lines[3]}" = "F-pipe=3 second=|" ]
  [ "${lines[4]}" = "F-escaped=2 second=b" ]
  [ "${lines[5]}" = "F-dot=0" ]
  [ "${lines[6]}" = "default=2 first=a" ]
  [ "${lines[7]}" = "implied=b" ]
}

@test "-l copies the record separator when parsed: -ln0 ends output with newline, -0l with NUL" {
  run_lab "$LAB" ors-order
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ln0: ors=10 rs=0" ]
  [ "${lines[1]}" = "0l: ors=0 rs=0" ]
  [ "${lines[2]}" = "00l: ors=10.10 rs-empty=1" ]
  [ "${lines[3]}" = "record=<a>" ]
  [ "${lines[4]}" = "record=<b>" ]
}

@test "-0777 slurps for cross-line edits, -00 reads paragraphs, -g exists from perl 5.36" {
  run_lab "$LAB" slurp
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ab" ]
  [ "${lines[1]}" = "c" ]
  [ "${lines[2]}" = "para=7" ]
  [ "${lines[3]}" = "para=2" ]
  if perl_at_least 5.036; then
    [ "${lines[4]}" = "g=4" ]
  else
    [ "${lines[4]}" = "g=rejected" ]
  fi
}

@test "\$. runs on across files unless ARGV is closed at eof, and eof() sees only the last file" {
  run_lab "$LAB" eof "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "cumulative f1:2" ]
  [ "${lines[1]}" = "cumulative f2:3" ]
  [ "${lines[2]}" = "reset f1:2" ]
  [ "${lines[3]}" = "reset f2:1" ]
  [ "${lines[4]}" = "last-file-only c" ]
}

@test "<> runs 'cmd |' and truncates '>file' arguments; <<>> and -i treat them as names" {
  run_lab "$LAB" inject "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "diamond=[INJECTED]" ]
  [ "${lines[1]}" = "double-diamond=[]" ]
  [ "${lines[2]}" = "inplace=[]" ]
  [ "${lines[3]}" = "victim-bytes=0" ]
  [ "${lines[4]}" = "victim-bytes-after-double-diamond=8" ]
}

@test "-i keeps the original on die or nonzero exit but commits a truncated file on exit 0 or last" {
  run_lab "$LAB" inplace-fail "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case ${lines[0]} in
    "die status=0 "*) false ;;
    "die status="*" lines=3 files=f") ;;
    *) false ;;
  esac
  [ "${lines[1]}" = "exit3 status=3 lines=3 files=f" ]
  [ "${lines[2]}" = "exit0 status=0 lines=1 files=f" ]
  [ "${lines[3]}" = "last status=0 lines=1 files=f" ]
}

@test "-i replaces a symlink and breaks a hard link, rewrites a 0444 file, and links the backup" {
  run_lab "$LAB" inplace-links "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "link=regular link-content=new target-content=old" ]
  [ "${lines[1]}" = "h1=new h2=old" ]
  [ "${lines[2]}" = "ro status=0 content=new mode=0444" ]
  [ "${lines[3]}" = "bk=new bk.orig=old edited-inode=new backup-inode=original" ]
}

@test "-i in a read-only directory warns, skips the file, and still exits 0" {
  run_lab "$LAB" inplace-dir "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "status=0 content=old warned=yes" ]
}

@test "-Mwarnings=FATAL,inplace turns a skipped missing or uneditable file into a nonzero exit" {
  run_lab "$LAB" fatal-inplace "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case ${lines[0]} in
    "missing-file status=0") false ;;
    "missing-file status="*) ;;
    *) false ;;
  esac
  case ${lines[1]} in
    "read-only-dir status=0 "*) false ;;
    "read-only-dir status="*" content=old") ;;
    *) false ;;
  esac
}

@test "a missing input exits 0; write errors surface as -p destination (EBADF) or at the exit flush" {
  run_lab "$LAB" exit-status "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "missing-file status=0" ]
  [ "${lines[1]}" = "p-small status=1 error=Unable to flush stdout: Bad file descriptor" ]
  [ "${lines[2]}" = "p-big status=9 error=-p destination: Bad file descriptor" ]
  [ "${lines[3]}" = "n-big status=1 error=Unable to flush stdout: Bad file descriptor" ]
}

@test "perl counts bytes by default and ignores the locale; -CS decodes, -CSL only in a UTF-8 locale" {
  run_lab "$LAB" unicode
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "default=2" ]
  [ "${lines[1]}" = "CS=1" ]
  [ "${lines[2]}" = "utf8-locale-no-C=2" ]
  [ "${lines[3]}" = "CSL-utf8-locale=1" ]
  [ "${lines[4]}" = "CSL-C-locale=2" ]
  [ "${lines[5]}" = "uc-default=unchanged" ]
  [ "${lines[6]}" = "uc-CS=changed" ]
}

@test "perl prints numbers with 15 significant digits where awk's OFMT keeps 6" {
  run_lab "$LAB" numbers
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "sum=0.3" ]
  [ "${lines[1]}" = "equal=no" ]
  [ "${lines[2]}" = "big=1234567.891" ]
  [ "${lines[3]}" = "pow=9.00719925474099e+15" ]
  [ "${lines[4]}" = "literal=9007199254740993" ]
  [ "${lines[5]}" = "awk-big=1.23457e+06" ]
}

@test "a non-numeric field adds 0 silently, and only -w reports it" {
  run_lab "$LAB" coerce
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "quiet sum=12" ]
  case ${lines[1]} in
    'Argument "-" isn'\''t numeric in addition (+) at -e line 1, <> line 2.') ;;
    *) false ;;
  esac
  [ "${lines[2]}" = "warned sum=12" ]
  [ "${#lines[@]}" -eq 3 ]
}

@test "a (?{ }) code block arriving through an interpolated pattern is refused at run time" {
  run_lab "$LAB" regex-eval
  [ "$status" -eq 0 ]
  case $output in
    "status=0 "*) false ;;
    "status="*" result=refused") ;;
    *) false ;;
  esac
}

@test "shell interpolation turns a value into Perl code; %ENV, @ARGV and -s keep it data" {
  run_lab "$LAB" shell-inject
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "1" ]
  [ "${lines[1]}" = "INJECTED" ]
  [ "${lines[2]}" = 'env=1; print "INJECTED"' ]
  [ "${lines[3]}" = 'argv=1; print "INJECTED"' ]
  [ "${lines[4]}" = 's=1; print "INJECTED"' ]
  [ "${#lines[@]}" -eq 5 ]
}

@test "a single quote reaches perl through '\\'' or as the escape \\x27" {
  run_lab "$LAB" quoting
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "close-reopen=it's" ]
  [ "${lines[1]}" = "hex-escape=it's" ]
  [ "${lines[2]}" = "q-braces=it's" ]
}

@test "an unknown mode is a usage error" {
  run_lab "$LAB" no-such-mode
  [ "$status" -eq 2 ]
}

@test "a mode that writes files refuses to run without a scratch directory" {
  run_lab "$LAB" inplace-fail
  [ "$status" -eq 2 ]
}
