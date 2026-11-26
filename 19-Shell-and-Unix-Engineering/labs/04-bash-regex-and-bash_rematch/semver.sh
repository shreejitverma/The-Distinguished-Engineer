#!/usr/bin/env bash
# semver.sh: split each VERSION argument into its Semantic Versioning 2.0.0 fields with [[ =~ ]].
# Usage: semver.sh VERSION...
# Prints one line per argument: "VERSION: major=X minor=Y patch=Z pre=P build=B" (absent parts are
# empty), or "VERSION: invalid".
# Exit status: 0 when every argument parsed, 1 when any was invalid, 2 when this shell cannot run it.
# Runs under bash 3.2 or later and under zsh; any other shell gets status 2 and a message.

if [ -n "${ZSH_VERSION-}" ]; then
  # zsh fills BASH_REMATCH (instead of MATCH and match) only with BASH_REMATCH set,
  # and indexes it from 0 only with KSH_ARRAYS set.
  setopt BASH_REMATCH KSH_ARRAYS
elif [ -z "${BASH_VERSION-}" ]; then
  printf 'semver.sh: needs bash or zsh for [[ =~ ]] and BASH_REMATCH\n' >&2
  exit 2
fi

# Bracket classes such as [[:alpha:]] follow LC_CTYPE; pin them to ASCII.
LC_ALL=C

# The semver.org regular expression, translated from PCRE to POSIX ERE:
# \d becomes [[:digit:]], (?:...) becomes a capturing (...), so every group below gets a number.
num='(0|[1-9][[:digit:]]*)'                                          # 1 group
pre_id='(0|[1-9][[:digit:]]*|[[:digit:]]*[[:alpha:]-][[:alnum:]-]*)' # 1 group
build_id='[[:alnum:]-]+'                                             # 0 groups
re="^$num\\.$num\\.$num(-($pre_id(\\.$pre_id)*))?(\\+($build_id(\\.$build_id)*))?\$"
# Count the open parentheses to find each field:
#   1 major  2 minor  3 patch  4 -pre  5 pre  6 first pre id  7 .id  8 last pre id
#   9 +build  10 build  11 last .id
PRE=5
BUILD=10

# Canary: a version that must match. A pattern the C library cannot compile makes [[ =~ ]] return 2
# in bash but 1 in zsh, so test a known-good input instead of trusting the status of real input.
if ! [[ 1.0.0-rc.1+b.2 =~ $re ]]; then
  printf 'semver.sh: this C library rejects the pattern: %s\n' "$re" >&2
  exit 2
fi

rc=0
for v in "$@"; do
  if [[ $v =~ $re ]]; then
    # Copy out at once: the next [[ =~ ]] anywhere, including in a called function, overwrites it.
    printf '%s: major=%s minor=%s patch=%s pre=%s build=%s\n' "$v" \
      "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" \
      "${BASH_REMATCH[PRE]}" "${BASH_REMATCH[BUILD]}"
  else
    printf '%s: invalid\n' "$v"
    rc=1
  fi
done
exit "$rc"
