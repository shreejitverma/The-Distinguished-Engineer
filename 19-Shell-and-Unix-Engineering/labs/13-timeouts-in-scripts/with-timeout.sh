#!/bin/sh
# with-timeout: run a command under a deadline with only POSIX sh, kill, and sleep.
# Usage: with-timeout.sh [-k GRACE] SECONDS COMMAND [ARG...]
# Exit status: the command's own status if it ends in time; 124 if the deadline
# passed (SIGTERM was sent, then SIGKILL GRACE seconds later if it was still
# running); 125 on a usage error. Durations are whole seconds (POSIX sleep).
# Limits: it signals the command's PID only, never its descendants, and the
# watchdog is a sibling of the command, not its parent (see the note).

grace=2
while getopts k: opt; do
  case $opt in
    k) grace=$OPTARG ;;
    *) exit 125 ;;
  esac
done
shift $((OPTIND - 1))
if [ "$#" -lt 2 ]; then
  printf 'usage: with-timeout.sh [-k GRACE] SECONDS COMMAND [ARG...]\n' >&2
  exit 125
fi
secs=$1
shift
case $secs:$grace in
  *[!0-9:]* | :* | *:)
    printf 'with-timeout: durations must be whole seconds\n' >&2
    exit 125
    ;;
  *) ;;
esac

"$@" &
cmd=$!

# The watchdog. Its exit status reports whether it fired: 124 once it has sent
# SIGTERM, 0 if it was cancelled first. Its standard streams go to /dev/null so
# that its sleep never holds open a pipe the caller is reading, such as $(...).
(
  rc=0
  sleeper=
  # shellcheck disable=SC2317,SC2329 # runs when the main shell cancels the watchdog
  trap 'kill "$sleeper" 2>/dev/null; exit "$rc"' TERM
  /bin/sleep "$secs" &
  sleeper=$!
  wait "$sleeper"
  rc=124
  kill -TERM "$cmd" 2>/dev/null || exit "$rc"
  /bin/sleep "$grace" &
  sleeper=$!
  wait "$sleeper"
  kill -KILL "$cmd" 2>/dev/null
  exit "$rc"
) </dev/null >/dev/null 2>&1 &
watchdog=$!

# Silence the "Terminated" job notice that bash 3.2, dash, and ksh93 print when
# wait reaps a job the watchdog killed. (Not "status": zsh reserves that name.)
wait "$cmd" 2>/dev/null
cmd_rc=$?
kill -TERM "$watchdog" 2>/dev/null
wait "$watchdog"
if [ "$?" -eq 124 ]; then
  exit 124
fi
exit "$cmd_rc"
