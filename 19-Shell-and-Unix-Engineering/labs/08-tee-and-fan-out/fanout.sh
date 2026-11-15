#!/bin/sh
# fanout.sh - probes for tee and fan-out.
# Part of 19-Shell-and-Unix-Engineering/08-Coreutils-and-the-Userland/tee-and-Fan-Out.
# Usage: fanout.sh MODE DIR
#   MODE is copy, append, dash, openfail, clobber, sigpipe, ignored, pflag, lockstep, or fifo.
#   DIR is an existing empty scratch directory; every file the probe makes goes there.
set -u

mode=${1:?usage: fanout.sh MODE DIR}
dir=${2:?usage: fanout.sh MODE DIR}
n=200000

# ksh93u+m joins pipeline stages with socketpair(2) unless the posix option is on;
# with sockets a reader that exits can give the writer ECONNRESET instead of SIGPIPE.
case ${KSH_VERSION-} in
  *93u+m*)
    # shellcheck disable=SC3040 # only reached in ksh93u+m, which has the option
    set -o posix
    ;;
  *) ;;
esac

# gen: print the integers 1..n, one per line (1288895 bytes for n = 200000),
# which is far more than a pipe holds (64 KiB on Linux and macOS).
gen() {
  awk -v n="$n" 'BEGIN { for (i = 1; i <= n; i++) print i }'
}

# lines FILE: print the number of lines in FILE without wc's leading blanks.
lines() {
  awk 'END { print NR }' "$1"
}

# bytes FILE: print the size of FILE in bytes without wc's leading blanks.
bytes() {
  wc -c <"$1" | awk '{ print $1 }'
}

# verdict FILE: say whether FILE holds the whole payload.
verdict() {
  got=$(lines "$1")
  if [ "$got" -eq "$n" ]; then echo complete; else echo truncated; fi
}

# status_word: classify the status that a pipeline stage saved in $dir/status.
status_word() {
  s=$(cat "$dir/status")
  if [ "$s" -gt 128 ]; then echo "killed by a signal"; else echo "exit $s"; fi
}

# report: print whether $dir/f is complete (or absent) and how the tee stage ended.
report() {
  if [ -e "$dir/f" ]; then v=$(verdict "$dir/f"); else v=absent; fi
  w=$(status_word)
  printf 'f: %s\ntee: %s\n' "$v" "$w"
}

case $mode in
  copy)
    # One read, then the same bytes to stdout and to every file operand, in full.
    gen | tee "$dir/a" "$dir/b" >"$dir/out"
    gen >"$dir/ref"
    for f in out a b; do
      if cmp -s "$dir/ref" "$dir/$f"; then echo "$f: identical"; else echo "$f: differs"; fi
    done
    ;;
  append)
    # Without -a, tee opens with O_TRUNC; with -a, with O_APPEND. -i is accepted everywhere.
    printf 'old\n' >"$dir/f"
    printf 'new\n' | tee "$dir/f" >/dev/null
    printf 'more\n' | tee -i -a "$dir/f" >/dev/null
    f=$(tr '\n' ' ' <"$dir/f")
    echo "f: $f"
    ;;
  dash)
    # POSIX: the operand - names a file called -; BusyBox tee treats it as stdout.
    (
      cd "$dir" || exit 1
      printf 'x\n' | tee - >out
      copies=$(lines out)
      echo "stdout copies: $copies"
      if [ -e ./- ]; then echo "file named -: yes"; else echo "file named -: no"; fi
    )
    ;;
  openfail)
    # An operand that cannot be opened is diagnosed, the rest still get the data.
    printf 'a\n' | tee "$dir/missing/x" "$dir/ok" >"$dir/out" 2>"$dir/err"
    echo "status: $?"
    ok=$(cat "$dir/ok")
    out=$(cat "$dir/out")
    diags=$(lines "$dir/err")
    printf 'ok: %s\nstdout: %s\ndiagnostics: %s\n' "$ok" "$out" "$diags"
    ;;
  clobber)
    # tee truncates its operands when it opens them, before it reads one byte.
    printf 'precious\n' >"$dir/f"
    # shellcheck disable=SC2094 # reading and writing the same file is the point
    tee "$dir/f" <"$dir/f" >"$dir/out"
    f=$(bytes "$dir/f")
    out=$(bytes "$dir/out")
    printf 'f: %s bytes\nstdout: %s bytes\n' "$f" "$out"
    ;;
  sigpipe)
    # head exits after one line; tee's next write to stdout raises SIGPIPE,
    # which kills tee before the file operand has the whole stream.
    gen | {
      tee "$dir/f"
      echo "$?" >"$dir/status"
    } | head -n 1 >/dev/null
    report
    ;;
  ignored)
    # SIGPIPE ignored: the write fails with EPIPE instead, and tee keeps feeding the file.
    gen | {
      trap '' PIPE
      tee "$dir/f" 2>"$dir/err"
      echo "$?" >"$dir/status"
    } | head -n 1 >/dev/null
    report
    if [ -s "$dir/err" ]; then echo "diagnostics: some"; else echo "diagnostics: none"; fi
    ;;
  pflag)
    # GNU and uutils -p: ignore SIGPIPE, drop a broken pipe output, keep the rest.
    gen | {
      tee -p "$dir/f" 2>"$dir/err"
      echo "$?" >"$dir/status"
    } | head -n 1 >/dev/null
    report
    ;;
  lockstep)
    # The slow consumer opens the FIFO at once but reads only after 2 s.
    # The payload exceeds the pipe buffer, so tee blocks on the FIFO and the
    # fast consumer (wc) cannot finish before the slow one starts reading.
    mkfifo "$dir/slow"
    {
      /bin/sleep 2
      : >"$dir/started"
      cat >/dev/null
    } <"$dir/slow" &
    gen | tee "$dir/slow" | wc -l >"$dir/count"
    if [ -e "$dir/started" ]; then early=no; else early=yes; fi
    wait
    count=$(awk '{ print $1 }' "$dir/count")
    echo "fast consumer lines: $count"
    echo "fast consumer done before slow one read: $early"
    ;;
  fifo)
    # Portable fan-out: start every reader first, then tee into the FIFOs, then wait.
    mkfifo "$dir/p1" "$dir/p2"
    awk 'END { print NR }' <"$dir/p1" >"$dir/c1" &
    grep -c '0$' <"$dir/p2" >"$dir/c2" &
    gen | tee "$dir/p1" "$dir/p2" >/dev/null
    wait
    c1=$(cat "$dir/c1")
    c2=$(cat "$dir/c2")
    printf 'consumer 1 (lines): %s\nconsumer 2 (lines ending in 0): %s\n' "$c1" "$c2"
    ;;
  *)
    echo "fanout.sh: unknown mode: $mode" >&2
    exit 2
    ;;
esac
