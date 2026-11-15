#!/bin/sh
# retry.sh: run a command until it succeeds, sleeping a capped, jittered exponential backoff between attempts.
# Usage: retry.sh [-n ATTEMPTS] [-b BASE_MS] [-c CAP_MS] [-j none|full|equal|decorr] [-d DEADLINE_S]
#                 [-r CODES] [-p] -- COMMAND [ARG...]
#   -n  total attempts, the first one included (default 5)
#   -b  base delay in milliseconds (default 1000)
#   -c  cap on any single delay in milliseconds (default 30000)
#   -j  jitter: none, full (the default), equal, or decorr
#   -d  give up instead of sleeping past DEADLINE_S seconds after the start (default 0: no deadline)
#   -r  space-separated exit statuses to retry (default: every status 1-125, and 255)
#   -p  plan: print the delay before each retry and run nothing
# Exit status: 0 on success; the command's last status when attempts run out or the status is not
# retryable; 128+N when the command was killed by signal N; 2 for a usage error.
# Randomness: 32 bits read from /dev/urandom by od (RETRY_URANDOM names another file); if that
# fails, awk seeded with the epoch second plus the PID of a fresh process.

usage() {
  printf '%s\n' "usage: retry.sh [-n attempts] [-b base_ms] [-c cap_ms] [-j none|full|equal|decorr] [-d deadline_s] [-r codes] [-p] -- command [arg...]" >&2
  exit 2
}

log() {
  printf 'retry: %s\n' "$*" >&2
}

is_uint() {
  case $1 in
    '' | *[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

# rand32: set rnd to a random integer in [0, 2^32).
rand32() {
  rnd=$(od -An -N4 -tu4 "${RETRY_URANDOM:-/dev/urandom}" 2>/dev/null | tr -d ' \n')
  is_uint "$rnd" && return 0
  # Fallback: awk's srand() with no argument seeds from the epoch second (POSIX), so two
  # processes started in the same second would draw the same value; mix in a fresh PID.
  # shellcheck disable=SC2016 # $$ belongs to the child shell, which prints its own PID
  pid=$(sh -c 'echo "$$"')
  rnd=$(awk -v pid="$pid" 'BEGIN {
    srand(); now = srand()          # the second call returns the seed the first one set
    srand((now + pid) % 2147483647)
    printf "%d\n", rand() * 4294967296
  }')
}

# ceiling K: set ceil to min(cap, base * 2^(K-1)), doubling stepwise so it never overflows.
ceiling() {
  ceil=$base
  i=1
  while [ "$i" -lt "$1" ] && [ "$ceil" -lt "$cap" ]; do
    ceil=$((ceil * 2))
    i=$((i + 1))
  done
  if [ "$ceil" -gt "$cap" ]; then ceil=$cap; fi
}

# next_delay K: set delay to the sleep in milliseconds before retry K.
next_delay() {
  ceiling "$1"
  case $jitter in
    none) delay=$ceil ;;
    full)
      rand32
      delay=$((rnd % (ceil + 1)))
      ;;
    equal)
      rand32
      half=$((ceil / 2))
      delay=$((half + rnd % (ceil - half + 1)))
      ;;
    decorr)
      # Brooker's decorrelated jitter: min(cap, uniform(base, 3 * previous sleep)).
      rand32
      delay=$((base + rnd % (prev * 3 - base + 1)))
      if [ "$delay" -gt "$cap" ]; then delay=$cap; fi
      prev=$delay
      ;;
    *) usage ;;
  esac
}

# msleep MS: sleep MS milliseconds; round up to whole seconds where sleep takes only integers (POSIX).
msleep() {
  if [ "$frac" = yes ]; then
    sleep "$(($1 / 1000)).$(printf '%03d' $(($1 % 1000)))"
  else
    sleep $((($1 + 999) / 1000))
  fi
}

retryable() {
  if [ -z "$codes" ]; then
    [ "$1" -le 125 ] || [ "$1" -eq 255 ]
    return
  fi
  case " $codes " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

attempts=5 base=1000 cap=30000 jitter=full deadline=0 codes='' plan=no
while getopts n:b:c:j:d:r:p opt; do
  case $opt in
    n) attempts=$OPTARG ;;
    b) base=$OPTARG ;;
    c) cap=$OPTARG ;;
    j) jitter=$OPTARG ;;
    d) deadline=$OPTARG ;;
    r) codes=$OPTARG ;;
    p) plan=yes ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))

for v in "$attempts" "$base" "$cap" "$deadline"; do
  is_uint "$v" || usage
done
case $codes in
  *[!0-9\ ]*) usage ;;
  *) ;;
esac
case $jitter in
  none | full | equal | decorr) ;;
  *) usage ;;
esac
if [ "$attempts" -lt 1 ] || [ "$cap" -lt "$base" ]; then usage; fi
prev=$base

if [ "$plan" = yes ]; then
  k=1
  while [ "$k" -lt "$attempts" ]; do
    next_delay "$k"
    printf 'retry %d: %d ms\n' "$k" "$delay"
    k=$((k + 1))
  done
  exit 0
fi

[ "$#" -gt 0 ] || usage
frac=no
if sleep 0.001 2>/dev/null; then frac=yes; fi
start=$(date +%s)
attempt=1
while :; do
  "$@"
  st=$?
  if [ "$st" -eq 0 ]; then exit 0; fi
  # 128+N in most shells, 256+N in ksh93, 384+N in yash; 255 is an ordinary status (ssh uses it).
  sig=0
  if [ "$st" -gt 256 ]; then
    sig=$((st % 128))
  elif [ "$st" -gt 128 ] && [ "$st" -lt 255 ]; then
    sig=$((st - 128))
  fi
  if [ "$sig" -ne 0 ]; then
    log "attempt $attempt was killed by signal $sig; not retrying"
    exit $((128 + sig))
  fi
  if ! retryable "$st"; then
    log "attempt $attempt failed with status $st, which is not retryable"
    exit "$st"
  fi
  if [ "$attempt" -ge "$attempts" ]; then
    log "attempt $attempt failed with status $st; giving up after $attempts attempts"
    exit "$st"
  fi
  next_delay "$attempt"
  if [ "$deadline" -gt 0 ]; then
    now=$(date +%s)
    if [ $((now - start + (delay + 999) / 1000)) -gt "$deadline" ]; then
      log "attempt $attempt failed with status $st; a $delay ms sleep would pass the ${deadline}s deadline"
      exit "$st"
    fi
  fi
  log "attempt $attempt failed with status $st; retrying in $delay ms"
  msleep "$delay"
  attempt=$((attempt + 1))
done
