#!/bin/sh
# rematch.sh: does this shell have [[ STRING =~ ERE ]], and does \1 in the ERE ^(a)\1$
# act as a back-reference (matches aa) or as a literal 1 (matches a1)?
# The ERE sits in a variable so the shell passes it through unquoted, as the engine sees it.
# Prints "no [[ =~ ]]", or one line per subject: aa=yes|no and a1=yes|no.
# The [[ ]] code is inside eval so shells without it fail at run time, not while parsing.
# The detection eval runs in a subshell: mksh has [[ but no =~, and a syntax error inside
# eval exits a non-interactive mksh, which would end the script with no output.
# shellcheck disable=SC2034 # re is read inside the eval string below
re='^(a)\1$'
if ! (eval '[[ a =~ ^a$ ]]') 2>/dev/null; then
  echo 'no [[ =~ ]]'
  exit 0
fi
for s in aa a1; do
  if eval '[[ $s =~ $re ]]'; then r=yes; else r=no; fi
  printf '%s=%s\n' "$s" "$r"
done
