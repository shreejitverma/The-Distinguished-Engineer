#!/bin/sh
# The architecture gate from the injected stage-2 script, next to the reading its author probably intended.
# usage: gate.sh BUILD_TRIPLET
# Prints "as-written=exit|continue intended=exit|continue".
if [ "$#" -ne 1 ]; then
  echo "usage: gate.sh BUILD_TRIPLET" >&2
  exit 2
fi
build=$1

# Quoted from Andres Freund's oss-security post of 2024-03-29: `!` binds to the first pipeline only.
if ! (echo "$build" | grep -Eq "^x86_64" >/dev/null 2>&1) && (echo "$build" | grep -Eq "linux-gnu$" >/dev/null 2>&1); then
  written='exit'
else
  written='continue'
fi

# What the line reads as: require x86_64 and linux-gnu.
if (echo "$build" | grep -Eq "^x86_64" >/dev/null 2>&1) && (echo "$build" | grep -Eq "linux-gnu$" >/dev/null 2>&1); then
  intended='continue'
else
  intended='exit'
fi
echo "as-written=$written intended=$intended"
