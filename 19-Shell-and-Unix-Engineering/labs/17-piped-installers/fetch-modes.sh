#!/bin/sh
# fetch-modes.sh: run one truncated download through each way people execute an installer.
# Usage: fetch-modes.sh MODE FIXTURE CUT SHELL [SHELL_ARG...]
# fake-curl.sh delivers the first CUT bytes of FIXTURE and exits 18, as curl does for a short body.
#   pipe      fake-curl | SHELL        the one-liner; the pipeline status is the shell's
#   pipefail  the same under `set -o pipefail`, or "pipefail=unsupported" if the shell lacks it
#   eval      eval "$(fake-curl)"      command substitution; the status of the download is masked
#   checked   body=$(fake-curl) || stop, and only then feed SHELL
# Every mode ends with a "status=N" line, except a refused "checked" run, which prints
# "fetch-failed=N" and exits 1.
set -u
mode=$1
fixture=$2
cut=$3
shift 3
curl=$(dirname -- "$0")/fake-curl.sh
case $mode in
  pipe)
    "$curl" cut "$fixture" "$cut" | "$@"
    echo "status=$?"
    ;;
  pipefail)
    # shellcheck disable=SC3040 # pipefail is POSIX.1-2024 but not in older shells; the subshell probes for it
    if (set -o pipefail) 2>/dev/null; then
      # shellcheck disable=SC3040 # reached only when the probe above succeeded
      set -o pipefail
      "$curl" cut "$fixture" "$cut" | "$@"
      echo "status=$?"
    else
      echo "pipefail=unsupported"
    fi
    ;;
  eval)
    # shellcheck disable=SC2312 # the masked status of the download is the point of this mode
    (eval "$("$curl" cut "$fixture" "$cut")")
    echo "status=$?"
    ;;
  checked)
    body=$("$curl" cut "$fixture" "$cut") || {
      echo "fetch-failed=$?"
      exit 1
    }
    printf '%s\n' "$body" | "$@"
    echo "status=$?"
    ;;
  *)
    echo "usage: fetch-modes.sh pipe|pipefail|eval|checked FIXTURE CUT SHELL [ARG...]" >&2
    exit 64
    ;;
esac
