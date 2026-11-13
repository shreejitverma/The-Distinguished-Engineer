#!/bin/sh
# badop.sh: does an unknown ${name@op} operator stop a non-interactive shell, or only the command?
# Prints "after STATUS" only when the shell survives the failed expansion.
# Usage: badop.sh
# The bad expansion sits in an eval string so that shells which parse the whole file first still start.
# shellcheck disable=SC2034 # x is read by the eval string below
x=1
eval 'printf "%s\n" "${x@Z}"'
printf 'after %s\n' "$?"
