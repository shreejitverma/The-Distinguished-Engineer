#!/bin/sh
# feed.sh: pipe a script text into a shell, the way `curl URL | sh` does.
# Usage: feed.sh SCRIPT_TEXT SHELL [SHELL_ARG...]
# SCRIPT_TEXT plus a newline goes to the shell's standard input; extra arguments such as
# `-s -- a b` are the shell's own, so the text sees them as $1, $2.
set -u
text=$1
shift
printf '%s\n' "$text" | "$@"
