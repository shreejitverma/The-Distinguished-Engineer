#!/bin/sh
# gone.sh PID SECONDS: wait up to SECONDS for PID to end; print "gone" or "running".
# A zombie counts as gone: kill -0 succeeds on a zombie, so it alone cannot tell a killed
# process that is waiting to be reaped from one that is still running.
pid=$1
tenths=$(($2 * 10))
while :; do
  state=
  if [ -r "/proc/$pid/stat" ]; then
    state=$(cut -d ' ' -f 3 "/proc/$pid/stat" 2>/dev/null)
  elif kill -0 "$pid" 2>/dev/null; then
    state=$(ps -o stat= -p "$pid" 2>/dev/null)
  fi
  case $state in
    "" | Z*)
      echo gone
      exit 0
      ;;
    *) ;;
  esac
  if [ "$tenths" -le 0 ]; then
    echo running
    exit 0
  fi
  tenths=$((tenths - 1))
  /bin/sleep 0.1
done
