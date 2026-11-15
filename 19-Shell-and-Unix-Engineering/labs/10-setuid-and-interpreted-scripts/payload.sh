# shellcheck shell=sh
# payload.sh - the file an attacker points BASH_ENV or ENV at.
# A shell that sources it before running a script prints the marker below first.
echo payload-ran
