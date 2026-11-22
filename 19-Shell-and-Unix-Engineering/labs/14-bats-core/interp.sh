#!/bin/sh
# interp: say whether the interpreter running this script is bash, decided from BASH_VERSION.
# bats runs the test in bash and this script in whatever shell the test's `run` names.
if [ -n "${BASH_VERSION-}" ]; then
  echo bash
else
  echo not-bash
fi
