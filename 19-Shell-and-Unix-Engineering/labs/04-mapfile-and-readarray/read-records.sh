#!/usr/bin/env bash
# read-records.sh CMD [ARG...]: run CMD, load its NUL-terminated records into an indexed array,
# print the record count and each record between angle brackets, and exit with CMD's status.
# mapfile returns 0 at end of input whatever the producer did, so the producer's status comes
# from `wait "$!"`, which can wait for a process substitution in bash 4.4 and later.
# Example: read-records.sh printf 'a b\0c\nd\0'
#   -> 2 records
#   -> <a b>
#   -> <c
#   -> d>
case ${BASH_VERSION-} in
  4.[4-9]* | [5-9].*) ;;
  *)
    printf 'read-records.sh: needs bash 4.4 or later\n' >&2
    exit 2
    ;;
esac

# read_records NAME CMD [ARG...]: fill the indexed array NAME with CMD's NUL-terminated records,
# without the NULs; a final record with no NUL is kept. Returns CMD's exit status.
# NAME must not be "name", which is local here.
read_records() {
  local name=$1
  shift
  # shellcheck disable=SC2312 # the status is not masked: wait "$!" returns it on the next line
  mapfile -t -d '' "$name" < <("$@")
  wait "$!"
}

records=()
status=0
read_records records "$@" || status=$?
printf '%d records\n' "${#records[@]}"
for r in "${records[@]}"; do
  printf '<%s>\n' "$r"
done
exit "$status"
