#!/bin/sh
# select-key-unsafe.sh FILE KEY: the ANTI-PATTERN. Splices KEY into the jq program text.
# A KEY such as  x" or true or "  rewrites the filter. It exists only so a test can show that.
exec jq -c --stream "select(length == 2 and .[0][0] == \"$2\")" -- "$1"
