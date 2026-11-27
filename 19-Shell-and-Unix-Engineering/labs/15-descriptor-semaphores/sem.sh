# shellcheck shell=sh
# sem.sh - a counting semaphore made of a FIFO opened read-write on descriptor 3.
# Source it with: . ./sem.sh
#
#   sem_open FIFO N   create FIFO, open it on descriptor 3, queue N tokens (one newline each)
#   sem_wait          take one token; blocks while the semaphore is 0
#   sem_post          put one token back
#
# The semaphore value is the number of bytes queued in the FIFO.
# Descriptor 3 stays open in this shell and in every subshell and background job it forks.

sem_open() {
  mkfifo -m 600 "$1" || return 1
  exec 3<>"$1" || return 1
  sem_left=$2
  while [ "$sem_left" -gt 0 ]; do
    printf '\n' >&3 || return 1
    sem_left=$((sem_left - 1))
  done
}

sem_wait() {
  # shellcheck disable=SC2034 # the token value is unused, taking it is the point
  read -r sem_token <&3
}

sem_post() {
  printf '\n' >&3
}
