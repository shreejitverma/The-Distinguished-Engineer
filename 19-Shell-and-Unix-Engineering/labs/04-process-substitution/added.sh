#!/usr/bin/env bash
# added.sh MODE OLD NEW: print the lines of NEW that are not in OLD (comm -13 of both sorted).
#   naive:   comm -13 <(sorted OLD) <(sorted NEW); a producer that fails is invisible.
#   checked: open each substitution on its own descriptor, keep its PID from $!, run comm,
#            then wait for each producer and fail if either one failed.
# Needs bash 5.1 or later: bash 4.4 can wait only for the most recent process substitution
# (waiting on the first PID fails with status 127), and before 5.1 POSIX mode has no
# process substitution at all.

if [ -z "${BASH_VERSION-}" ]; then
  echo "added: needs bash 5.1 or later" >&2
  exit 3
fi
case $BASH_VERSION in
  [0-4].* | 5.0.*)
    echo "added: needs bash 5.1 or later" >&2
    exit 3
    ;;
  *) ;;
esac

export LC_ALL=C # one collation for sort and comm

sorted() {
  if [ ! -r "$1" ]; then
    printf 'added: cannot read %s\n' "$1" >&2
    return 66
  fi
  sort -- "$1"
}

naive() {
  # shellcheck disable=SC2312 # hiding the producers' status is the bug this mode shows
  comm -13 <(sorted "$1") <(sorted "$2")
}

checked() {
  local old new old_pid new_pid comm_status old_status new_status
  # shellcheck disable=SC2312 # each producer's status is collected below with wait
  exec {old}< <(sorted "$1")
  old_pid=$!
  # shellcheck disable=SC2312 # collected below with wait
  exec {new}< <(sorted "$2")
  new_pid=$!
  comm -13 "/dev/fd/$old" "/dev/fd/$new"
  comm_status=$?
  exec {old}<&- {new}<&-
  wait "$old_pid"
  old_status=$?
  wait "$new_pid"
  new_status=$?
  if [ "$old_status" -ne 0 ] || [ "$new_status" -ne 0 ]; then
    printf 'added: producer failed (old=%d new=%d)\n' "$old_status" "$new_status" >&2
    return 2
  fi
  return "$comm_status"
}

case ${1-} in
  naive | checked) "$1" "$2" "$3" ;;
  *)
    echo "usage: added.sh naive|checked OLD NEW" >&2
    exit 2
    ;;
esac
