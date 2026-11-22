#!/bin/sh
# attempt.sh NAME DIR: try one action that a restricted shell forbids, and print
# "NAME: allowed" or "NAME: blocked", decided by the real effect of the action.
# DIR is a scratch directory holding the fixtures (lib.sh and bin/plain, bin/hashbang).
# The caller decides whether the shell is restricted: `run_lab -r attempt.sh NAME DIR`.
# The script uses no output redirection (restricted shells forbid it), so error messages
# from a blocked action stay on stderr for the caller to see.
name=${1:?usage: attempt.sh NAME DIR}
dir=${2:?usage: attempt.sh NAME DIR}

try_cd() {
  cd / || return 1
  out=$(pwd) || return 1
  [ "$out" = / ]
}
try_path() {
  PATH=$dir
  [ "$PATH" = "$dir" ]
}
try_shellvar() {
  SHELL=/bin/sh
  [ "$SHELL" = /bin/sh ]
}
try_histfile() {
  HISTFILE=$dir/hist
  [ "$HISTFILE" = "$dir/hist" ]
}
try_ifs() {
  IFS=:
  [ "$IFS" = : ]
}
try_slash() {
  out=$(/bin/echo ok) || return 1
  [ "$out" = ok ]
}
try_commandp() {
  out=$(command -p echo ok) || return 1
  [ "$out" = ok ]
}
try_exec() {
  # shellcheck disable=SC2116 # the exec builtin is the point, not an echo
  out=$(exec echo ok) || return 1
  [ "$out" = ok ]
}
try_redirect() {
  echo data >"$dir/out"
  [ -s "$dir/out" ]
}
try_append() {
  echo data >>"$dir/app"
  [ -s "$dir/app" ]
}
try_read() {
  read -r first <"$dir/lib.sh"
  [ -n "$first" ]
}
try_dot() {
  # shellcheck disable=SC1091 # the fixture is created by the test at run time
  . "$dir/lib.sh"
  [ "${LAB_DOT-}" = ran ]
}
try_setplus() {
  # shellcheck disable=SC3041 # -r is not POSIX; that is what this attempt probes
  set +r
  cd / || return 1
  out=$(pwd) || return 1
  [ "$out" = / ]
}
try_shescape() {
  out=$(sh -c "cd / && pwd") || return 1
  [ "$out" = / ]
}
try_envescape() {
  out=$(env sh -c "cd / && pwd") || return 1
  [ "$out" = / ]
}
try_awkescape() {
  out=$(awk 'BEGIN { system("cd / && pwd") }') || return 1
  [ "$out" = / ]
}
try_plain() {
  out=$(plain) || return 1
  [ "$out" = / ]
}
try_hashbang() {
  out=$(hashbang) || return 1
  [ "$out" = / ]
}

case $name in
  cd | path | shellvar | histfile | ifs | slash | commandp | exec) ;;
  redirect | append | read | dot | setplus) ;;
  shescape | envescape | awkescape | plain | hashbang) ;;
  *)
    echo "attempt.sh: unknown attempt: $name" >&2
    exit 2
    ;;
esac

# A subshell keeps a fatal error (for example an assignment to a readonly variable) and any
# change of directory or variables from leaking into the next attempt.
if ("try_$name"); then
  echo "$name: allowed"
else
  echo "$name: blocked"
fi
