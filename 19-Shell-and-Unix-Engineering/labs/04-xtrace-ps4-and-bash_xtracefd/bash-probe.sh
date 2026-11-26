#!/bin/sh
# bash-probe.sh: bash-only probes of BASH_XTRACEFD and of how bash formats its trace.
# Prints "needs-bash" and exits 0 under any other shell.
# Bash syntax that a POSIX parser rejects lives in eval strings, because mksh parses
# a whole script before running it.
# Usage: bash-probe.sh MODE FILE
#   reassign   assigning a new BASH_XTRACEFD leaves the old descriptor open
#   unset      unset BASH_XTRACEFD closes the descriptor it named
#   closefd    closing the trace descriptor with exec N>&- sends the trace back to stderr
#   invalid    the diagnostic for a BASH_XTRACEFD that names no open descriptor
#   silence    { set +x; } 2>/dev/null versus 3>/dev/null when the trace goes to fd 3
#   replicate  the first character of PS4 repeats once per eval level
#   ctrl       how a traced argument containing a tab is quoted
#   assign     how scalar and array assignments are traced

if [ -z "${BASH_VERSION-}" ]; then
  echo needs-bash
  exit 0
fi

mode=${1-}
f=${2-}
if [ -z "$f" ]; then
  echo "usage: bash-probe.sh MODE FILE" >&2
  exit 2
fi

# state FD: print fdN=open or fdN=closed for descriptor FD of this shell.
state() {
  if (true >&"$1") 2>/dev/null; then
    echo "fd$1=open"
  else
    echo "fd$1=closed"
  fi
}

case $mode in
  reassign)
    exec 3>/dev/null 4>/dev/null
    BASH_XTRACEFD=3
    BASH_XTRACEFD=4
    state 3
    state 4
    ;;
  unset)
    exec 4>/dev/null
    BASH_XTRACEFD=4
    unset BASH_XTRACEFD
    state 4
    ;;
  closefd)
    exec 5>/dev/null
    BASH_XTRACEFD=5
    exec 5>&-
    PS4='+ '
    (
      set -x
      : after-close
    ) 2>"$f"
    if grep -F -e ': after-close' "$f" >/dev/null; then echo stderr; else echo lost; fi
    ;;
  invalid)
    exec 9>&-
    (BASH_XTRACEFD=9) 2>"$f"
    sed -e 's/^.*BASH_XTRACEFD/BASH_XTRACEFD/' "$f"
    ;;
  silence)
    PS4='+ '
    (
      BASH_XTRACEFD=3
      set -x
      : one
      { set +x; } 2>/dev/null
      set -x
      : two
      { set +x; } 3>/dev/null
    ) 3>"$f" 2>/dev/null
    cat "$f"
    ;;
  replicate)
    PS4='>x '
    (
      set -x
      eval ': e1; eval ": e2"'
    ) 2>"$f"
    cat "$f"
    ;;
  ctrl)
    tab=$(printf 'a\tb')
    PS4='+ '
    (
      set -x
      : "$tab"
    ) 2>"$f"
    awk '{ gsub(/\t/, "<TAB>"); print }' "$f"
    ;;
  assign)
    PS4='+ '
    (
      set -x
      eval 'v="a b"; arr=(x "y z")'
    ) 2>"$f"
    cat "$f"
    ;;
  *)
    echo "bash-probe.sh: unknown mode: $mode" >&2
    exit 2
    ;;
esac
