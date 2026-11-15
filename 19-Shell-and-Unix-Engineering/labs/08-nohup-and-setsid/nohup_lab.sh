#!/bin/sh
# nohup_lab.sh MODE PROBE DIR: what nohup and setsid change, run under the shell under test.
# Every mode works inside DIR (a fresh scratch directory) and prints computed results.
# The child modes run inside a pseudo-terminal made by "PROBE pty"; they start
# the shell under test again from LAB_SHELL (split on spaces, as labs/lib/lab.bash does).

set -u
mode=$1
probe=$2
dir=$3
shell=${LAB_SHELL:-sh}

# proc_comm PID: the command name of PID (procfs on Linux, ps elsewhere).
proc_comm() {
  if [ -r "/proc/$1/comm" ]; then
    /bin/cat "/proc/$1/comm"
  else
    ps -o comm= -p "$1"
  fi
}

# wait_sleep PID: poll for up to 5 s until PID has exec'd sleep.
wait_sleep() {
  tries=50
  while [ "$tries" -gt 0 ]; do
    comm=$(proc_comm "$1" 2>/dev/null) || comm=
    case $comm in
      *sleep) return 0 ;;
      *) ;;
    esac
    tries=$((tries - 1))
    sleep 0.1
  done
  return 1
}

# gone PID: true once PID has exited (reaped, or a zombie not reaped yet).
gone() {
  kill -0 "$1" 2>/dev/null || return 0
  if [ -r "/proc/$1/status" ]; then
    state=$(awk '$1 == "State:" { print $2 }' "/proc/$1/status" 2>/dev/null) || return 0
  else
    state=$(ps -o stat= -p "$1" 2>/dev/null) || return 0
  fi
  case $state in
    Z*) return 0 ;;
    *) return 1 ;;
  esac
}

# wait_gone PID: poll for up to 5 s until PID has exited.
wait_gone() {
  tries=50
  while [ "$tries" -gt 0 ]; do
    gone "$1" && return 0
    tries=$((tries - 1))
    sleep 0.1
  done
  return 1
}

cd "$dir" || exit 1

case $mode in
  hup-disposition)
    # Output goes to a pipe, so nohup must not create nohup.out or print a notice.
    plain=$("$probe" hup </dev/null)
    nohupped=$(nohup "$probe" hup </dev/null 2>&1)
    printf 'plain: %s\n' "$plain"
    printf 'nohup: %s\n' "$nohupped"
    if [ -e nohup.out ]; then echo "nohup.out: present"; else echo "nohup.out: absent"; fi
    ;;
  exit-status)
    printf 'echo never\n' >noexec
    nohup /bin/sh -c 'exit 3' </dev/null >/dev/null 2>&1
    utility=$?
    nohup ./noexec </dev/null >/dev/null 2>&1
    noexec=$?
    nohup ./missing </dev/null >/dev/null 2>&1
    missing=$?
    echo "utility=$utility noexec=$noexec missing=$missing"
    ;;
  internal-failure)
    nohup </dev/null >/dev/null 2>&1
    plain=$?
    POSIXLY_CORRECT=1 nohup </dev/null >/dev/null 2>&1
    posixly=$?
    echo "no-operand=$plain posixly-correct=$posixly"
    ;;
  nohup-on-tty)
    # stdin, stdout, and stderr are all the pty slave here.
    LC_ALL=C "$probe" pty 10 nohup "$probe" stdio
    if [ -e nohup.out ]; then
      listing=$(ls -l nohup.out)
      perms=${listing%% *}
      content=$(/bin/cat nohup.out)
      printf 'mode=%s\n' "${perms%[@+.]}"
      printf 'nohup.out: %s\n' "$content"
    else
      echo "nohup.out: absent"
    fi
    ;;
  hangup)
    # shellcheck disable=SC2086,SC2248 # LAB_SHELL is a command plus arguments
    "$probe" pty-hangup READY 15 $shell "$0" hangup-child "$probe" "$dir"
    for name in plain nohup newsession; do
      pid=$(/bin/cat "$name.pid")
      if wait_gone "$pid"; then
        echo "$name=dead"
      else
        echo "$name=alive"
        kill -KILL "$pid" 2>/dev/null
      fi
    done
    ;;
  hangup-child)
    # Session leader on the pty: three background sleeps, then wait for the hangup.
    /bin/sleep 30 >/dev/null 2>&1 &
    echo "$!" >plain.pid
    nohup /bin/sleep 30 >/dev/null 2>&1 &
    echo "$!" >nohup.pid
    "$probe" newsession /bin/sleep 30 >/dev/null 2>&1 &
    echo "$!" >newsession.pid
    for name in plain nohup newsession; do
      pid=$(/bin/cat "$name.pid")
      wait_sleep "$pid" || exit 1
    done
    echo READY
    wait
    exit 0
    ;;
  ctty)
    # shellcheck disable=SC2086,SC2248 # LAB_SHELL is a command plus arguments
    "$probe" pty 10 $shell "$0" ctty-child "$probe" "$dir"
    for name in plain nohup newsession; do
      line=$(/bin/cat "$name.txt")
      printf '%s: %s\n' "$name" "$line"
    done
    ;;
  ctty-child)
    "$probe" session >plain.txt
    nohup "$probe" session >nohup.txt 2>/dev/null
    "$probe" newsession "$probe" session >newsession.txt
    exit 0
    ;;
  setsid-leader)
    # "probe leader" makes the tool a process group leader, so setsid(2) would fail in it.
    if command -v setsid >/dev/null 2>&1; then
      echo "tool=setsid"
      # shellcheck disable=SC2016 # $0 belongs to the inner sh
      "$probe" leader setsid /bin/sh -c 'sleep 1; exec "$0" ids' "$probe" >ids.txt 2>&1
    else
      echo "tool=probe"
      # shellcheck disable=SC2016 # $0 belongs to the inner sh
      "$probe" leader "$probe" newsession /bin/sh -c 'sleep 1; exec "$0" ids' "$probe" >ids.txt 2>&1
    fi
    status=$?
    if [ -s ids.txt ]; then early=no; else early=yes; fi
    tries=50
    while [ ! -s ids.txt ] && [ "$tries" -gt 0 ]; do
      tries=$((tries - 1))
      sleep 0.1
    done
    ids=$(/bin/cat ids.txt)
    pid=${ids#pid=}
    pid=${pid%% *}
    sid=${ids##*sid=}
    if [ "$pid" = "$sid" ]; then leads=yes; else leads=no; fi
    echo "status=$status returned_early=$early child_leads_session=$leads"
    ;;
  setsid-wait)
    if ! command -v setsid >/dev/null 2>&1; then
      echo "setsid: absent"
      exit 0
    fi
    "$probe" leader setsid -w /bin/sh -c 'exit 7' 2>/dev/null
    code=$?
    # shellcheck disable=SC2016 # $$ belongs to the inner shell
    "$probe" leader setsid -w /bin/sh -c 'kill -TERM $$' 2>/dev/null
    killed=$?
    echo "exit=$code killed=$killed"
    ;;
  trap-ignored)
    # shellcheck disable=SC2016,SC2086,SC2248 # $$ is the inner shell's; LAB_SHELL is a command plus arguments
    nohup $shell -c 'trap "echo caught" HUP; kill -HUP $$; echo survived' </dev/null 2>&1
    ;;
  *)
    echo "usage: $0 MODE PROBE DIR" >&2
    exit 2
    ;;
esac
