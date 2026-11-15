#!/bin/sh
# victim.sh - stands in for a privileged script: it runs one external command by name.
# Usage: victim.sh FILE
# Prints "victim:" followed by the first line of FILE as read by whatever "cat" resolves to.
# An environment that defines a bash function named cat, or a BASH_ENV file, changes what runs.
content=$(cat "$1")
printf 'victim:%s\n' "$content"
