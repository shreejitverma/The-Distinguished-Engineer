#!/bin/sh
# suppression-audit.sh [-m MAX] FILE...: list ShellCheck suppressions that carry no reason.
# A directive is a comment line "# shellcheck disable=SC1234,SC5678 # why this is safe";
# the text after the second # is the reason. Other directives (source=, shell=, enable=) are ignored.
# Prints "FILE:LINE: no reason for CODES" per unexplained directive, then
# "suppressions=N unexplained=M" where N counts codes. With -m, N above MAX is also a failure,
# which makes the suppression count a ratchet that review can only tighten.
# Exit status: 0 clean, 1 unexplained or over MAX, 2 usage error or unreadable file.
max=
case ${1-} in
  -m)
    max=${2-}
    case $max in
      '' | *[!0-9]*)
        echo "suppression-audit.sh: -m needs a number" >&2
        exit 2
        ;;
      *) ;;
    esac
    shift 2
    ;;
  *) ;;
esac
if [ "$#" -eq 0 ]; then
  echo "usage: suppression-audit.sh [-m MAX] FILE..." >&2
  exit 2
fi

total=0
unexplained=0
for file in "$@"; do
  if [ ! -r "$file" ]; then
    echo "suppression-audit.sh: cannot read $file" >&2
    exit 2
  fi
  n=0
  # The || test keeps a last line that has no trailing newline.
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    body=${line#"${line%%[![:space:]]*}"}
    case $body in
      '#'*) ;;
      *) continue ;;
    esac
    body=${body#'#'}
    body=${body#"${body%%[![:space:]]*}"}
    case $body in
      'shellcheck '*disable=*) ;;
      *) continue ;;
    esac
    rest=${body#*disable=}
    codes=${rest%%[[:space:]]*}
    after=${rest#"$codes"}
    reason=
    case $after in
      *'#'*) reason=${after#*'#'} ;;
      *) ;;
    esac
    # Count the comma-separated codes without field splitting (native zsh does not split).
    list=$codes
    while [ -n "$list" ]; do
      total=$((total + 1))
      case $list in
        *,*) list=${list#*,} ;;
        *) list= ;;
      esac
    done
    # A reason is any non-blank text after the second #.
    case $reason in
      *[![:space:]]*) ;;
      *)
        unexplained=$((unexplained + 1))
        printf '%s:%d: no reason for %s\n' "$file" "$n" "$codes"
        ;;
    esac
  done <"$file"
done

printf 'suppressions=%d unexplained=%d\n' "$total" "$unexplained"
if [ -n "$max" ] && [ "$total" -gt "$max" ]; then
  printf 'suppressions=%d exceeds max=%d\n' "$total" "$max"
  exit 1
fi
[ "$unexplained" -eq 0 ]
