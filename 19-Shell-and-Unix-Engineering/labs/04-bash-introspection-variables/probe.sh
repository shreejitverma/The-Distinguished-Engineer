#!/bin/sh
# probe.sh: portable probes for shell introspection variables; runs under every shell in the matrix.
# Usage: probe.sh MODE
#   support  which bash introspection names this shell provides (FUNCNAME is read inside a function)
#   pid      whether $$ and PPID keep the parent's values inside ( ) and $( ), as POSIX requires
set -u

# isset NAME: print "set" if the variable NAME is set, else "unset".
isset() {
  eval "if [ -n \"\${$1+x}\" ]; then echo set; else echo unset; fi"
}

# show_funcname: print the value FUNCNAME has inside a function (empty where the shell has none).
show_funcname() {
  # shellcheck disable=SC3028 # reading FUNCNAME is the probe; a shell without it gives empty
  printf '%s' "${FUNCNAME-}"
}

case ${1-} in
  support)
    fn=$(show_funcname)
    pid=$(isset BASHPID)
    sub=$(isset BASH_SUBSHELL)
    es=$(isset EPOCHSECONDS)
    er=$(isset EPOCHREALTIME)
    sr=$(isset SRANDOM)
    echo "FUNCNAME=$fn BASHPID=$pid BASH_SUBSHELL=$sub EPOCHSECONDS=$es EPOCHREALTIME=$er SRANDOM=$sr"
    ;;
  pid)
    top_pid=$$
    top_ppid=$PPID
    sub=$(
      if [ "$$" = "$top_pid" ]; then d=same; else d=changed; fi
      if [ "$PPID" = "$top_ppid" ]; then p=same; else p=changed; fi
      echo "$d $p"
    )
    (
      if [ "$$" = "$top_pid" ]; then d=same; else d=changed; fi
      if [ "$PPID" = "$top_ppid" ]; then p=same; else p=changed; fi
      echo "paren: dollar=$d ppid=$p"
    )
    echo "comsub: dollar=${sub%% *} ppid=${sub#* }"
    ;;
  *)
    echo "usage: probe.sh support|pid" >&2
    exit 2
    ;;
esac
