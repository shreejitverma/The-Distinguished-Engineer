# shellcheck shell=bash
# The PS4 method: kcov writes this file as bash-helper.sh and points BASH_ENV at it.
# Every traced command then starts with a marker: kcov@FILE@LINE@
# shellcheck disable=SC2016 # PS4 must expand when each trace line is printed, not now
PS4='kcov@${BASH_SOURCE}@${LINENO}@'
set -x
