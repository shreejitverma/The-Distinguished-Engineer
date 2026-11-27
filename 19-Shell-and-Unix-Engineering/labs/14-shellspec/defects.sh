#!/bin/sh
# defects.sh: a simplified excerpt of the shell-defect probes in ShellSpec's libexec/shellspec-inspection.sh
# (ShellSpec 0.28.1). Prints the name of each probe that fires, one per line, then "done".
# A probe fires when this shell has the defect ShellSpec works around.

# Defect EMPTYPARAMS: "$@" with no parameters fails under set -u.
if ! (
  set -eu --
  : "$@"
) 2>/dev/null; then
  echo EMPTYPARAMS
fi

# Defect REDEFINE: a function redefined inside a command substitution leaks into the parent shell.
redefine() { echo redefine; }
redefined=$(
  redefine() { :; }
  redefine
)
if [ "$redefined" = "redefine" ]; then
  echo REDEFINE
fi

# Defect BUILTIN: a builtin cannot be shadowed by a function.
if ! (
  false() { :; }
  # shellcheck disable=SC2310 # whether this name shadows the builtin is the probe
  false
) 2>/dev/null; then
  echo BUILTIN
fi

# Defect SHELLFLAG: set -u does not make an unset variable an error.
# shellcheck disable=SC2154 # v is unset on purpose
if (
  set -u
  unset v || :
  : "$v"
) 2>/dev/null; then
  echo SHELLFLAG
fi

# Defect ERREXIT: errexit is not ignored for a function on the left of &&, so the function stops early.
errexit_out=$(
  errexit() {
    set -e
    false
    :
  }
  # shellcheck disable=SC2310 # the left side of && must suppress errexit for this probe
  errexit && echo OK
)
if [ -z "$errexit_out" ]; then
  echo ERREXIT
fi

# Defect SUBSHELL: "return 2" from a function under set -e in a subshell reports 1.
set +e
(
  set -e
  subshell() { return 2; }
  subshell
) 2>/dev/null
subshell_status=$?
if [ "$subshell_status" -eq 1 ]; then
  echo SUBSHELL
fi

# Defect SETE: set -e done through eval does not show up in $-.
eval "set -e"
sete=$-
if [ "$sete" = "${sete%e*}" ]; then
  echo SETE
fi
set +e

# Capability DEBUG_TRAP: the shell accepts a DEBUG trap (ShellSpec's coverage needs it).
# shellcheck disable=SC3047 # DEBUG is not POSIX; probing for it is the point
if (trap '' DEBUG) 2>/dev/null; then
  echo DEBUG_TRAP
fi

echo "done"
