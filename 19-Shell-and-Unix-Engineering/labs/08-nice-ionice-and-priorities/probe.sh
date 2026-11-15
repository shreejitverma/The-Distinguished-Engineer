#!/bin/sh
# probe.sh MODE PRIO: show what nice, renice, and ionice do to the nice value and I/O priority.
# PRIO is the compiled prio.c probe. The shell under test runs this script, but the utilities are
# whatever nice, renice, and ionice its PATH finds, so every mode reports the implementation too.
#   flavor TOOL   gnu, uutils, util-linux, busybox, bsd, absent, or unknown
#   default       nice with no options: base and new nice value
#   clamp         nice -n 100: the kernel clamps to its maximum
#   negative      nice -n 1 nice -n -1: an unprivileged attempt to lower the nice value
#   inherit       nice deltas of foreground, subshell, and background children of this shell
#   exit          exit status of nice for a shell function and for a non-executable file
#   renice        renice -n 1 twice, then twice more with POSIXLY_CORRECT=1
#   ionice        Linux only: raw ioprio after each ionice form, per installed implementation
#   ulimit-e      the shell's ulimit -e (RLIMIT_NICE) next to the value prio reads

set -u

mode=${1-}
prio=${2-}

# ionice_run ARG...: the ionice implementation named by $impl.
ionice_run() {
  if [ "$impl" = busybox ]; then
    busybox ionice "$@"
  else
    ionice "$@"
  fi
}

flavor() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo absent
    return 0
  fi
  banner=$({
    "$1" --version
    "$1" --help
  } 2>&1 </dev/null)
  case $banner in
    *uutils*) echo uutils ;;
    *"GNU coreutils"*) echo gnu ;;
    *util-linux*) echo util-linux ;;
    *BusyBox*) echo busybox ;;
    *"invalid nice value"* | *"priority argument"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

case $mode in
  flavor)
    flavor "$prio"
    ;;
  default)
    base=$("$prio" self)
    new=$(nice "$prio" self)
    echo "base=$base new=$new"
    ;;
  clamp)
    base=$("$prio" self)
    new=$(nice -n 100 "$prio" self)
    echo "base=$base new=$new"
    ;;
  negative)
    base=$("$prio" self)
    inner=$(nice -n 1 nice -n -1 "$prio" self 2>/dev/null)
    st=$?
    warning=$(nice -n 1 nice -n -1 "$prio" self 2>&1 >/dev/null)
    if [ -n "$warning" ]; then warned=yes; else warned=no; fi
    echo "base=$base inner=${inner:-none} status=$st warned=$warned"
    ;;
  inherit)
    fg=$("$prio" self)
    sub=$( ("$prio" self) )
    bg=$(
      "$prio" self &
      wait
    )
    echo "subshell=+$((sub - fg)) background=+$((bg - fg))"
    ;;
  exit)
    f() { :; }
    nice f 2>/dev/null
    function_status=$?
    nice "${0%/*}/prio.c" 2>/dev/null
    nonexec_status=$?
    echo "function=$function_status nonexec=$nonexec_status"
    ;;
  renice)
    base=$("$prio" self)
    renice -n 1 -p "$$" >/dev/null 2>&1
    renice -n 1 -p "$$" >/dev/null 2>&1
    plain=$("$prio" self)
    POSIXLY_CORRECT=1 renice -n 1 -p "$$" >/dev/null 2>&1
    POSIXLY_CORRECT=1 renice -n 1 -p "$$" >/dev/null 2>&1
    posix=$("$prio" self)
    echo "base=$base plain=$plain posix=$posix"
    ;;
  ionice)
    found=no
    for impl in ionice busybox; do
      if [ "$impl" = busybox ]; then
        command -v busybox >/dev/null 2>&1 || continue
        busybox ionice -c 2 true >/dev/null 2>&1 || continue
        label=busybox
      else
        command -v ionice >/dev/null 2>&1 || continue
        label=$(flavor ionice)
      fi
      found=yes
      none=$("$prio" ioprio 2>/dev/null) || none=fail
      c3=$(ionice_run -c 3 "$prio" ioprio 2>/dev/null) || c3=fail
      c2=$(ionice_run -c 2 "$prio" ioprio 2>/dev/null) || c2=fail
      n7=$(ionice_run -n 7 "$prio" ioprio 2>/dev/null) || n7=fail
      # shellcheck disable=SC2016 # $1 belongs to the child shell
      kid=$(ionice_run -c 2 -n 6 /bin/sh -c '"$1" ioprio' sh "$prio" 2>/dev/null) || kid=fail
      echo "$label none=[$none] c3=[$c3] c2=[$c2] n7=[$n7] child=[$kid]"
    done
    if [ "$found" = no ]; then
      echo "no ionice"
      exit 1
    fi
    ;;
  ulimit-e)
    # shellcheck disable=SC3045 # ulimit -e is the non-POSIX option under test
    out=$(ulimit -e 2>&1)
    st=$?
    case $out in
      unlimited) ;;
      "" | *[!0-9]*) out=message ;;
      *) ;;
    esac
    rlimit=$("$prio" rlimit-nice)
    echo "out=$out status=$st rlimit=$rlimit"
    ;;
  *)
    echo "usage: probe.sh flavor|default|clamp|negative|inherit|exit|renice|ionice|ulimit-e PRIO" >&2
    exit 2
    ;;
esac
