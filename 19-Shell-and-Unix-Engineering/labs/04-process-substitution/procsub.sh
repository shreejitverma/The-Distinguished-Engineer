#!/bin/sh
# procsub.sh: one probe per process substitution behavior.
# Usage: procsub.sh MODE [ARG...]
#
# Every <(...) and >(...) sits in a single-quoted eval string, for two reasons:
# a shell without the syntax (dash, mksh, posh) then fails only in the mode that runs it,
# not while parsing this file, and shfmt cannot re-space the operators.
# "|| exit" after an eval stops a shell that reports the syntax error but keeps running.

# Close descriptors 3 to 9 so every shell starts from the same table: ksh93 hands out the
# lowest free descriptor, and a test harness (bats keeps fd 3) would otherwise shift it.
# A background consumer that inherited the harness's fd 3 would also keep the harness waiting.
exec 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-

mode=${1-}
if [ "$#" -gt 0 ]; then
  shift
fi

case $mode in
  kind)
    # A pathname word (process substitution), no word at all (yash process redirection),
    # or a syntax error.
    out=$(eval 'echo X <(:) Y' 2>/dev/null) || out=error
    case $out in
      "X /"*" Y") echo path ;;
      "X Y") echo redirection ;;
      *) echo none ;;
    esac
    ;;
  paths)
    # The words two substitutions in one command expand to.
    eval 'printf "%s\n" <(:) <(:)' || exit
    ;;
  read)
    # An external consumer opens each pathname and reads that producer's output.
    eval 'cat <(echo a) <(echo b)' || exit
    ;;
  filetype)
    # $1 is the compiled fdprobe: file type, seekability, and first line of the pathname.
    eval '"$1" stat <(echo hi)' || exit
    ;;
  closed)
    # $1 is fdprobe: it closes every descriptor from 3 up before opening the pathname,
    # as sudo does before it runs a command.
    eval '"$1" closeall <(echo hi)' || exit
    ;;
  tempfile)
    # zsh only: =(cmd) runs cmd to completion into a temporary regular file, which survives
    # a consumer that closes its descriptors and is removed when the command finishes.
    eval 'keep() { kept=$1; "$2" stat "$1"; "$2" closeall "$1"; }
      keep =(echo hi) "$1"
      if [ -e "$kept" ]; then echo after=kept; else echo after=removed; fi' || exit
    ;;
  status)
    # The producer exits 7; the consumer exits 0. What do $? and $! say?
    eval 'cat <(exit 7)
      echo "status=$?"
      case ${!-} in
        "") echo bang=unset ;;
        0) echo bang=zero ;;
        *) echo bang=pid ;;
      esac
      wait $! 2>/dev/null
      echo "wait=$?"' || exit
    ;;
  async)
    # $1 is a file the consumer creates one second after it starts; an external command.
    eval '/bin/echo x > >(/bin/sleep 1; cat >"$1")
      if [ -e "$1" ]; then echo present; else echo absent; fi' || exit
    ;;
  async-group)
    # The same redirection on a compound command, which the shell itself performs.
    eval '{ /bin/echo x; } > >(/bin/sleep 1; cat >"$1")
      if [ -e "$1" ]; then echo present; else echo absent; fi' || exit
    ;;
  async-wait)
    # As async-group, then wait for the process named by $!.
    eval '{ /bin/echo x; } > >(/bin/sleep 1; cat >"$1")
      wait $! 2>/dev/null
      if [ -e "$1" ]; then echo present; else echo absent; fi' || exit
    ;;
  async-waitall)
    # As async-group, then wait with no operands.
    eval '{ /bin/echo x; } > >(/bin/sleep 1; cat >"$1")
      wait
      if [ -e "$1" ]; then echo present; else echo absent; fi' || exit
    ;;
  async-wait-ext)
    # As async (external command), then wait for $!.
    eval '/bin/echo x > >(/bin/sleep 1; cat >"$1")
      wait $! 2>/dev/null
      if [ -e "$1" ]; then echo present; else echo absent; fi' || exit
    ;;
  capture)
    # Wrap the command in $(...): the consumer inherits the capture pipe as its stdout,
    # so the command substitution cannot finish before the consumer does.
    eval 'x=$(echo hi > >(/bin/sleep 1; cat))
      echo "got=$x"' || exit
    ;;
  capture-released)
    # As capture, but the consumer moves its stdout to $1 before it writes: the capture
    # ends when the consumer lets go of the pipe, not when the consumer finishes.
    eval ': "$(/bin/echo hi > >(exec >"$1"; /bin/sleep 1; cat))"
      if [ -s "$1" ]; then echo complete; else echo incomplete; fi' || exit
    ;;
  chain)
    # Two output substitutions on one command: where does the second one's stdout go?
    # $1 receives what the first consumer saw.
    eval '{ echo E >&2; } > >(sed "s/^/out:/" >"$1") 2> >(sed "s/^/err:/")' || exit
    /bin/sleep 1
    seen=$(cat "$1")
    echo "file=$seen"
    ;;
  stdin)
    # The inner cat reads its standard input: the pipe from echo, or the shell's /dev/null.
    eval '{ echo in | cat <(cat); } </dev/null' || exit
    ;;
  lifetime)
    # Keep the pathname past the command that created it, then try to read it.
    eval 'set -- <(echo hi)' || exit
    if data=$(cat "$1" 2>/dev/null); then
      echo "alive:$data"
    else
      echo dead
    fi
    ;;
  persist)
    # $1 is a descriptor number from 3 to 9: exec moves the pipe to a descriptor the
    # script owns, which outlives the command.
    case $1 in
      [3-9]) ;;
      *)
        echo "persist: descriptor must be 3 to 9" >&2
        exit 2
        ;;
    esac
    eval "exec $1< <(echo persistent)" || exit
    line=unread
    eval "read -r line <&$1"
    echo "line=$line"
    ;;
  loopvar)
    # The loop is not a pipeline stage, so its variable survives.
    eval 'n=0
      while read -r _; do n=$((n + 1)); done < <(printf "a\nb\n")
      echo "n=$n"' || exit
    ;;
  sigpipe)
    # The producer writes 1 MiB to a pipe nobody reads; $1 receives head's exit status.
    eval ': <(head -c 1048576 /dev/zero; echo "$?" >"$1")' || exit
    /bin/sleep 1
    seen=$(cat "$1")
    echo "producer=$seen"
    ;;
  count)
    # 70 substitutions in one command: one pipe and one descriptor each.
    subs=''
    i=0
    while [ "$i" -lt 70 ]; do
      subs="$subs <(:)"
      i=$((i + 1))
    done
    eval "set -- $subs" || exit
    if [ "$#" -eq 0 ]; then
      distinct=0
    else
      distinct=$(printf '%s\n' "$@" | sort -u | wc -l)
    fi
    echo "count=$# distinct=$((distinct))"
    ;;
  split)
    # Is the substituted pathname field split on a slash in IFS?
    eval 'IFS=/
      set -- <(:)
      echo "$#"' || exit
    ;;
  quoted)
    # Quoting turns the operator into text.
    echo "<(:)" '>(:)'
    ;;
  option)
    # A substitution as the tail of a larger word.
    eval 'echo --file=<(:)' || exit
    ;;
  source)
    # Run commands a producer writes: dot reads the pathname like a script file.
    eval '. <(echo echo sourced)' || exit
    echo "rc=$?"
    ;;
  errexit)
    # The producer fails; set -e cannot see it.
    eval 'set -e
      cat <(false)
      echo survived' || exit
    ;;
  orphan)
    # The producer sleeps one second, then creates $1; the script does not wait for it.
    eval ': <(exec 2>/dev/null; /bin/sleep 1; echo late >"$1")' || exit
    echo returned
    ;;
  *)
    echo "usage: procsub.sh MODE [ARG...]" >&2
    exit 2
    ;;
esac
