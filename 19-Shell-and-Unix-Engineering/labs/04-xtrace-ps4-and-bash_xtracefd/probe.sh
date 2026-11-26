#!/bin/sh
# probe.sh: portable probes of set -x and PS4, one mode per behavior.
# Every mode that traces sends the trace to FILE through a redirection on a ( ) subshell,
# never through $( ), because bash raises the PS4 repetition level inside $( ).
# The modes that summarize print a word computed from FILE, so the tests compare facts,
# not each shell's trace layout.
# Usage: probe.sh MODE FILE
#   default    print the startup value of PS4 as [VALUE]
#   expand     whether PS4 is parameter-expanded before each trace line
#   quote      the trace line for an argument that contains a space
#   nest       whether a trace from inside $( ) repeats the first PS4 character
#   setplusx   whether the set +x that turns tracing off is itself traced
#   hide       whether { set +x; } 2>/dev/null keeps that line out of the trace
#   ownredir   whether a command's own 2>/dev/null hides that command's trace line
#   follows    whether the trace follows a later exec 2>FILE or keeps the old stderr
#   funcscope  whether set -x inside a function stays on after the function returns
#   xtracefd   whether BASH_XTRACEFD=3 moves the trace to descriptor 3

mode=${1-}
f=${2-}

case $mode in
  default) ;;
  *)
    if [ -z "$f" ]; then
      echo "usage: probe.sh MODE FILE" >&2
      exit 2
    fi
    ;;
esac

# has LINE-FRAGMENT: succeed if FILE contains the fixed string once single quotes are
# removed, because yash traces ':' and '+x' quoted and BusyBox quotes the reserved word 'in'.
has() {
  tr -d "'" <"$f" | grep -F -e "$1" >/dev/null 2>&1
}

case $mode in
  default)
    printf '[%s]\n' "$PS4"
    ;;
  expand)
    v=7
    PS4='[$v] '
    (
      set -x
      : marker
    ) 2>"$f"
    cat "$f"
    ;;
  quote)
    PS4='+ '
    (
      set -x
      : marker 'c d'
    ) 2>"$f"
    grep -F -e marker "$f"
    ;;
  nest)
    PS4='+ '
    (
      set -x
      # shellcheck disable=SC2034 # only the trace of the assignment matters
      x=$(: inner)
    ) 2>"$f"
    if grep -e '^++ ' "$f" >/dev/null; then echo replicated; else echo flat; fi
    ;;
  setplusx)
    PS4='+ '
    (
      set -x
      set +x
    ) 2>"$f"
    if has 'set +x'; then echo traced; else echo untraced; fi
    ;;
  hide)
    PS4='+ '
    (
      set -x
      : before
      { set +x; } 2>/dev/null
    ) 2>"$f"
    if ! has ': before'; then
      echo no-trace
    elif has 'set +x'; then
      echo shown
    else
      echo hidden
    fi
    ;;
  ownredir)
    PS4='+ '
    (
      set -x
      : own 2>/dev/null
    ) 2>"$f"
    if has ': own'; then echo shown; else echo hidden; fi
    ;;
  follows)
    PS4='+ '
    (
      set -x
      exec 2>"$f"
      : after
    ) 2>/dev/null
    if has ': after'; then echo follows; else echo snapshot; fi
    ;;
  funcscope)
    PS4='+ '
    (
      tracer() {
        set -x
        : in
      }
      tracer
      : out
    ) 2>"$f"
    if ! has ': in'; then
      echo no-trace
    elif has ': out'; then
      echo kept
    else
      echo restored
    fi
    ;;
  xtracefd)
    PS4='+ '
    (
      BASH_XTRACEFD=3
      set -x
      : marker
    ) 3>"$f" 2>/dev/null
    if has ': marker'; then echo fd3; else echo stderr; fi
    ;;
  *)
    echo "probe.sh: unknown mode: $mode" >&2
    exit 2
    ;;
esac
