#!/bin/sh
# shellcheck disable=SC3047,SC3040,SC2310 # probing non-POSIX ERR, DEBUG and errtrace is the point; set -e is never on when has_cond runs
# probe.sh: portable probes of the ERR and DEBUG trap conditions, one mode per behavior.
# ERR and DEBUG are not POSIX conditions, so every mode first asks the shell under test
# whether it accepts the condition and prints "unsupported" when it does not.
# Shell-specific syntax ([[, ${.sh.command}, setopt, shopt) lives in eval strings so that
# shells that parse a whole script first (mksh) and POSIX parsers (dash) can still read this file.
# Usage: probe.sh MODE
#   support      print "ERR=yes|no DEBUG=yes|no"
#   status       ERR sees the failing status in $?, and $? survives the trap
#   exempt       which errexit-exempt contexts still fire ERR (only "last" should)
#   errexit      ERR runs before set -e exits, and the exit status is the failure's
#   funcall      how many times ERR fires for a function whose last command fails
#   errtrace     the same, after set -o errtrace (prints no-errtrace if the shell lacks it)
#   pipefail     whether a pipeline that fails only through pipefail fires ERR
#   cmdsub       whether a failure inside $( ) fires the parent's ERR trap
#   debug-order  DEBUG runs before each command and sees the previous command's status
#   debug-pipe   how many DEBUG runs a three-stage pipeline costs
#   debug-func   how many DEBUG runs a call to a two-command function costs
#   debug-skip   each shell's mechanism for skipping the next command from a DEBUG trap

has_cond() {
  (trap : "$1") 2>/dev/null
}

need() {
  if ! has_cond "$1"; then
    echo unsupported
    exit 0
  fi
}

case ${1-} in
  support)
    err=no dbg=no
    if has_cond ERR; then err=yes; fi
    if has_cond DEBUG; then dbg=yes; fi
    echo "ERR=$err DEBUG=$dbg"
    ;;
  status)
    need ERR
    trap 'echo "err:$?"' ERR
    /bin/sh -c 'exit 3'
    echo "after:$?"
    ;;
  exempt)
    need ERR
    tag=none
    trap 'echo "fired:$tag"' ERR
    tag=if
    if false; then :; fi
    tag=while
    while false; do :; done
    tag=until
    n=0
    until [ "$n" -eq 1 ]; do n=1; done
    tag=and
    false && :
    tag=or
    false || :
    tag=not
    ! false
    tag=pipe
    false | :
    tag=last
    : && false
    echo end
    ;;
  errexit)
    need ERR
    trap 'echo "err:$?"' ERR
    set -e
    /bin/sh -c 'exit 3'
    echo never
    ;;
  funcall)
    need ERR
    g() { false; }
    trap 'echo E' ERR
    g
    echo end
    ;;
  errtrace)
    need ERR
    if ! (set -o errtrace) 2>/dev/null; then
      echo no-errtrace
      exit 0
    fi
    set -o errtrace
    g() { false; }
    trap 'echo E' ERR
    g
    echo end
    ;;
  pipefail)
    need ERR
    if ! (set -o pipefail) 2>/dev/null; then
      echo no-pipefail
      exit 0
    fi
    set -o pipefail
    trap 'echo E' ERR
    false | :
    echo end
    ;;
  cmdsub)
    need ERR
    trap 'echo E >&2' ERR
    x=$(
      false
      echo ok
    )
    echo "x=$x"
    ;;
  debug-order)
    need DEBUG
    trap 'echo "D:$?"' DEBUG
    false
    true
    trap - DEBUG
    ;;
  debug-pipe)
    need DEBUG
    n=0
    trap 'n=$((n + 1))' DEBUG
    true | true | true
    trap - DEBUG
    echo "runs=$n"
    ;;
  debug-func)
    need DEBUG
    f() {
      :
      :
    }
    n=0
    trap 'n=$((n + 1))' DEBUG
    f
    trap - DEBUG
    echo "runs=$n"
    ;;
  debug-skip)
    need DEBUG
    if [ -n "${BASH_VERSION-}" ]; then
      # extdebug: any non-zero status from the DEBUG trap skips the command.
      eval 'shopt -s extdebug
        trap '\''[ "$BASH_COMMAND" != "echo skipped" ]'\'' DEBUG'
    elif [ -n "${ZSH_VERSION-}" ]; then
      # zsh: setting ERR_EXIT inside the DEBUG trap skips the command.
      eval 'trap '\''case $ZSH_DEBUG_CMD in "echo skipped") setopt errexit ;; *) : ;; esac'\'' DEBUG'
    else
      # ksh93: a DEBUG trap that exits with status 2 skips the command.
      eval 'trap '\''case ${.sh.command} in "echo skipped") (exit 2) ;; *) (exit 0) ;; esac'\'' DEBUG'
    fi
    echo skipped
    echo kept
    trap - DEBUG
    ;;
  *)
    echo "usage: probe.sh MODE (see the header)" >&2
    exit 2
    ;;
esac
