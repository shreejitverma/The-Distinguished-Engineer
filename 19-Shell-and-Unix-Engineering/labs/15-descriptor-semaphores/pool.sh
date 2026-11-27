#!/bin/sh
# pool.sh - run JOBS one-second jobs with at most N running at once.
# usage: pool.sh N JOBS LOG DIR      bounded by a descriptor semaphore
#        pool.sh unbounded JOBS LOG  control run: no semaphore
# Each job appends "s" to LOG when it starts and "e" just before it finishes (and, in the bounded
# run, before it returns its token), so the running count at any point in LOG never exceeds N.
# Prints "max=<peak running count> jobs=<jobs finished>", plus "tokens=<queued>" when FIFOTOOL names
# the fifotool binary.
set -u
here=$(dirname "$0")
# shellcheck source=sem.sh
. "$here/sem.sh"

n=${1:?usage: pool.sh N JOBS LOG DIR}
njobs=${2:?usage: pool.sh N JOBS LOG DIR}
log=${3:?usage: pool.sh N JOBS LOG DIR}
: >"$log"

if [ "$n" != unbounded ]; then
  dir=${4:?usage: pool.sh N JOBS LOG DIR}
  sem_open "$dir/sem" "$n" || exit 1
fi

i=0
while [ "$i" -lt "$njobs" ]; do
  if [ "$n" != unbounded ]; then
    sem_wait || exit 1
  fi
  {
    printf 's\n' >>"$log"
    /bin/sleep 1
    printf 'e\n' >>"$log"
    if [ "$n" != unbounded ]; then
      sem_post
    fi
  } &
  i=$((i + 1))
done
wait

peak=$(awk '$1 == "s" { c++; if (c > m) m = c } $1 == "e" { c-- } END { print m + 0 }' "$log") || exit 1
done_jobs=$(awk '$1 == "e" { c++ } END { print c + 0 }' "$log") || exit 1
printf 'max=%s jobs=%s\n' "$peak" "$done_jobs"
if [ "$n" != unbounded ] && [ -n "${FIFOTOOL-}" ]; then
  left=$("$FIFOTOOL" count "$dir/sem") || exit 1
  printf 'tokens=%s\n' "$left"
fi
