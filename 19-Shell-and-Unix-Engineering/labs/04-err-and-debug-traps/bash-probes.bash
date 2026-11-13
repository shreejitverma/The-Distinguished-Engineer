# shellcheck shell=bash
# bash-probes.bash: bash-only probes, sourced by bash-probe.sh after its "is this bash" guard.
# Modes (the first positional parameter):
#   stale-command      BASH_COMMAND in an ERR trap fired by a failing function call
#   errtrace-twice     with set -E, one failure inside a function fires ERR twice
#   cond-arith         [[ ]] and (( )) failures fire ERR (bash 4.1 and later)
#   subshell           a failing ( ) subshell fires ERR in the parent (bash 4.0 and later)
#   command-builtin    how many times "command false" fires ERR (once from bash 5.0)
#   extdebug-errtrace  whether shopt -u extdebug also clears set -E (bash 4.4 and later)
#   trapsig            BASH_TRAPSIG inside DEBUG and ERR traps (bash 5.3 and later)
#   trap-persists      a DEBUG trap set inside a function outlives the function
#   cmdsub-stdout      an ERR trap that writes to stdout pollutes a captured $( ) value
#   functrace          DEBUG runs for a call to a two-command function, with set -T
#   unexpanded         BASH_COMMAND in a DEBUG trap is the command text before expansion
#   return-trap        a RETURN trap set in a function persists and fires for a later "."

case ${1-} in
  stale-command)
    g() {
      false
    }
    trap 'echo "cmd=$BASH_COMMAND"' ERR
    g
    ;;
  errtrace-twice)
    set -E
    g() {
      false
    }
    trap 'echo "E:${FUNCNAME[0]:-top}"' ERR
    g
    ;;
  cond-arith)
    trap 'echo "E:$BASH_COMMAND"' ERR
    # shellcheck disable=SC2050 # a constant false comparison is the failure under test
    [[ a == b ]]
    ((0))
    echo end
    ;;
  subshell)
    trap 'echo "E:$?"' ERR
    (exit 5)
    echo end
    ;;
  command-builtin)
    trap 'echo E' ERR
    command false
    echo end
    ;;
  extdebug-errtrace)
    set -E
    shopt -s extdebug
    shopt -u extdebug
    case $- in
      *E*) echo errtrace-on ;;
      *) echo errtrace-off ;;
    esac
    ;;
  trapsig)
    trap 'printf "err=%s\n" "${BASH_TRAPSIG-unset}"' ERR
    trap 'printf "debug=%s\n" "${BASH_TRAPSIG-unset}"; trap - DEBUG' DEBUG
    false
    ;;
  trap-persists)
    setter() { trap 'echo "D:$BASH_COMMAND"' DEBUG; }
    setter
    echo after-setter
    trap - DEBUG
    ;;
  cmdsub-stdout)
    set -E
    trap 'echo TRAPPED' ERR
    v=$(
      false
      echo data
    )
    printf '[%s]\n' "$v"
    ;;
  functrace)
    f() {
      :
      :
    }
    set -T
    n=0
    trap 'n=$((n + 1))' DEBUG
    f
    trap - DEBUG
    echo "runs=$n"
    ;;
  unexpanded)
    v=world
    trap 'printf "D:%s\n" "$BASH_COMMAND"' DEBUG
    echo "hello $v"
    trap - DEBUG
    ;;
  return-trap)
    f() {
      trap 'echo "R:${FUNCNAME[0]:-top}"' RETURN
    }
    g() { :; }
    f
    g
    . /dev/null
    trap - RETURN
    ;;
  *)
    echo "usage: bash-probe.sh MODE (see bash-probes.bash)" >&2
    exit 2
    ;;
esac
