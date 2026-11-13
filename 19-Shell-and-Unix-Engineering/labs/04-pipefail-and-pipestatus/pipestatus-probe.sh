#!/bin/sh
# pipestatus-probe: mechanism probes for the per-stage status array
# (PIPESTATUS in bash and mksh, pipestatus in native zsh).
# Array syntax lives in eval strings so that dash, posh, and BusyBox ash can parse this file;
# every mode prints "unsupported" in a shell that has no such array.
# Usage: pipestatus-probe.sh basic
#        pipestatus-probe.sh reset
#        pipestatus-probe.sh piecewise
#        pipestatus-probe.sh bang
#        pipestatus-probe.sh collapse
#        pipestatus-probe.sh sigpipe
#        pipestatus-probe.sh many
#        pipestatus-probe.sh trap
#        pipestatus-probe.sh bashonly
# shellcheck disable=SC3040,SC3047 # pipefail and the ERR trap run only in shells that have the array

mode=${1:?mode}

# bash-specific probes run the bash binary under test ($BASH) in a fresh process.
if [ "$mode" = bashonly ]; then
  if [ -z "${BASH_VERSION-}" ]; then
    echo n/a
    exit 0
  fi
  # shellcheck disable=SC2016 # these expansions belong to the child bash
  imported=$(env PIPESTATUS=0 "$BASH" -c 'false | false; echo "${PIPESTATUS[*]} rc=$?"')
  # shellcheck disable=SC2016 # these expansions belong to the child bash
  first=$("$BASH" -c 'PIPESTATUS=x; false | (exit 2); echo "${PIPESTATUS[*]}"')
  # shellcheck disable=SC2016 # these expansions belong to the child bash
  later=$("$BASH" -c 'true; PIPESTATUS=x; false | (exit 2); echo "${PIPESTATUS[*]}"')
  # shellcheck disable=SC2016 # these expansions belong to the child bash
  shellopts=$(env SHELLOPTS=pipefail "$BASH" -c 'false | true; echo "$?"')
  # shellcheck disable=SC2016 # these expansions belong to the child bash
  scoped=$("$BASH" -c 'f() { local - 2>/dev/null; set -o pipefail; }; f; false | true; echo "$?"')
  echo "imported=[$imported] first=[$first] later=[$later] shellopts=$shellopts local_dash=$scoped"
  exit 0
fi

# Pick the array: zsh names it pipestatus (1-based), bash and mksh PIPESTATUS (0-based).
if [ -n "${ZSH_VERSION-}" ]; then
  v=pipestatus
  base=1
else
  v=PIPESTATUS
  base=0
fi
# snap copies the whole array in one simple command, before anything can reset it.
# shellcheck disable=SC2089,SC2090 # snap is code for eval, so its quotes are meant literally
snap="r=\"\${${v}[*]-}\""

if ! (eval 'a=(x)') 2>/dev/null; then
  echo unsupported
  exit 0
fi
# shellcheck disable=SC2312 # the per-stage statuses are the measurement
false | (exit 2)
eval "$snap"
if [ -z "$r" ]; then
  echo unsupported
  exit 0
fi

case $mode in
  basic)
    # shellcheck disable=SC2312 # the per-stage statuses are the measurement
    false | true | (exit 3)
    eval "$snap"
    echo "pipestatus=$r"
    ;;

  reset)
    # Any later simple command, even a builtin with no output, replaces the array.
    # shellcheck disable=SC2312 # the per-stage statuses are the measurement
    false | (exit 2)
    :
    eval "$snap"
    echo "pipestatus=$r"
    ;;

  piecewise)
    # Two separate assignments: the first one is itself a simple command.
    a='' b=''
    # shellcheck disable=SC2312 # the per-stage statuses are the measurement
    false | (exit 2)
    eval "a=\${${v}[$base]-unset}; b=\${${v}[$((base + 1))]-unset}"
    echo "first=$a second=$b"
    ;;

  bang)
    st=
    # ! inverts $? but the array keeps the raw statuses.
    # shellcheck disable=SC2312 # the per-stage statuses are the measurement
    ! false | true
    eval "st=\$? $snap"
    echo "status=$st pipestatus=$r"
    ;;

  collapse)
    # After eval or a function call bash holds that command's own status; zsh keeps the
    # function's last pipeline but not eval's, and mksh keeps the inner pipeline in both.
    f() {
      # shellcheck disable=SC2312 # the per-stage statuses are the measurement
      false | (exit 4)
    }
    f
    eval "$snap"
    fn=$r
    eval 'false | (exit 5)'
    eval "$snap"
    echo "function=[$fn] eval=[$r]"
    ;;

  sigpipe)
    # The writer has 1 MiB to write and the reader takes 1 byte, so the writer must block
    # on a full pipe and then die of SIGPIPE when the reader exits.
    # shellcheck disable=SC2312 # the per-stage statuses are the measurement
    head -c 1048576 /dev/zero | head -c 1 >/dev/null
    eval "$snap"
    echo "pipestatus=$r"
    ;;

  many)
    # 299 stages of : and a last stage that exits 7.
    n='' last='' rc=''
    p=
    i=0
    while [ "$i" -lt 299 ]; do
      p="$p : |"
      i=$((i + 1))
    done
    # shellcheck disable=SC2016 # expanded later, by eval
    if [ "$v" = pipestatus ]; then
      count='rc=$? n=${#pipestatus} last=${pipestatus[-1]}'
    else
      count='rc=$? n=${#PIPESTATUS[@]} last=${PIPESTATUS[${#PIPESTATUS[@]}-1]}'
    fi
    eval "$p (exit 7); $count"
    echo "stages=300 n=$n last=$last rc=$rc"
    ;;

  trap)
    # An ERR trap fired by a pipefail failure still sees the pipeline's array.
    set -o pipefail
    # shellcheck disable=SC2090 # snap is code for eval
    trap 'eval "$snap"; echo "trap=$r"' ERR
    # shellcheck disable=SC2312 # the failing upstream stage is the measurement
    false | true
    trap - ERR
    ;;

  *)
    echo "unknown mode: $mode" >&2
    exit 2
    ;;
esac
