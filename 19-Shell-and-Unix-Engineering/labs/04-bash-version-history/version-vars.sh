#!/bin/sh
# version-vars: print which shell identity variable the running shell sets.
# Usage: version-vars.sh
# Prints one "NAME=set" or "NAME=unset" line for each of the six variables shells use to
# announce themselves; a portable script detects its shell with these, not with $0 or ps.

for name in BASH_VERSION ZSH_VERSION KSH_VERSION YASH_VERSION POSH_VERSION BB_ASH_VERSION; do
  if eval "[ -n \"\${$name+x}\" ]"; then
    printf '%s=set\n' "$name"
  else
    printf '%s=unset\n' "$name"
  fi
done
