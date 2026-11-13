#!/bin/sh
# pipefail-probe: mechanism probes for set -o pipefail (POSIX.1-2024 XCU 2.9.2 and set).
# Every mode feature-detects pipefail first and prints "unsupported" in a shell without it.
# Usage: pipefail-probe.sh support
#        pipefail-probe.sh rightmost
#        pipefail-probe.sh table
#        pipefail-probe.sh sampled
#        pipefail-probe.sh errexit
#        pipefail-probe.sh waitall DIR
#        pipefail-probe.sh async
# shellcheck disable=SC3040 # pipefail is POSIX.1-2024; each use is feature-detected first

mode=${1:?mode}

# set -o is a special built-in, so an unknown option would abort this shell: probe in a subshell.
if (set -o pipefail) 2>/dev/null; then
  have=yes
else
  have=no
fi

if [ "$mode" != support ] && [ "$mode" != waitall ] && [ "$have" = no ]; then
  echo unsupported
  exit 0
fi

# pf_now: print on if pipefail is in effect in this shell right now, computed from a pipeline.
pf_now() {
  # shellcheck disable=SC2312 # the masked status is the measurement
  if false | true; then echo off; else echo on; fi
}

case $mode in
  support)
    echo "pipefail=$have"
    ;;

  rightmost)
    # The left stage fails last in time (after 1 s) and the middle stage fails first.
    # "Rightmost non-zero" gives 2; "last to exit non-zero" would give 3.
    set -o pipefail
    # shellcheck disable=SC2312 # the pipeline status is the measurement
    (
      /bin/sleep 1
      exit 3
    ) | (exit 2) | true
    echo "status=$?"
    ;;

  table)
    # The four rows of the XCU 2.9.2 exit-status table, plus ! over an all-zero pipeline.
    # shellcheck disable=SC2312 # the pipeline status is the measurement
    false | true
    off=$?
    # shellcheck disable=SC2312 # the pipeline status is the measurement
    ! false | true
    off_bang=$?
    set -o pipefail
    # shellcheck disable=SC2312 # the pipeline status is the measurement
    (exit 3) | true
    on=$?
    # shellcheck disable=SC2312 # the pipeline status is the measurement
    ! (exit 3) | true
    on_bang=$?
    # shellcheck disable=SC2312 # the pipeline status is the measurement
    ! true | true
    on_bang_ok=$?
    echo "off=$off off_bang=$off_bang on=$on on_bang=$on_bang on_bang_ok=$on_bang_ok"
    ;;

  sampled)
    # The XCU 2.9.2 example: the setting in force when the pipeline begins decides its status,
    # so a set -o pipefail in the last stage must not affect this pipeline's status.
    # Whether the option is on afterwards shows where the last stage ran.
    # shellcheck disable=SC2216,SC2312 # set in the last stage is the point; its status is the measurement
    false | set -o pipefail
    st=$?
    now=$(pf_now)
    echo "status=$st after=$now"
    ;;

  errexit)
    # set -e acts on the pipeline's status, which pipefail derives from every stage.
    set -e -o pipefail
    # shellcheck disable=SC2312 # the failing upstream stage is the measurement
    false | true
    echo survived
    ;;

  waitall)
    # Does the shell wait for an upstream stage that outlives the last stage?
    # POSIX requires waiting for the last command only; pipefail makes ksh93 wait for all.
    dir=${2:?dir}
    if [ "${3-}" = pipefail ]; then
      if [ "$have" = no ]; then
        echo unsupported
        exit 0
      fi
      set -o pipefail
    fi
    marker="$dir/late.$$"
    {
      /bin/sleep 1
      : >"$marker"
    } | true
    if [ -e "$marker" ]; then
      echo waited
    else
      echo not-waited
      # Let the orphaned stage finish so it never outlives the test.
      /bin/sleep 2
    fi
    ;;

  async)
    # When is pipefail sampled for a background pipeline that wait collects?
    # The last stage keeps the job alive past the option change, so wait finds it running.
    set -o pipefail
    # shellcheck disable=SC2216 # sleep only keeps the job alive; it reads nothing
    (exit 3) | /bin/sleep 1 &
    p=$!
    set +o pipefail
    wait "$p"
    start_only=$?
    # shellcheck disable=SC2216 # sleep only keeps the job alive; it reads nothing
    (exit 3) | /bin/sleep 1 &
    p=$!
    set -o pipefail
    wait "$p"
    wait_only=$?
    echo "start_only=$start_only wait_only=$wait_only"
    ;;

  *)
    echo "unknown mode: $mode" >&2
    exit 2
    ;;
esac
