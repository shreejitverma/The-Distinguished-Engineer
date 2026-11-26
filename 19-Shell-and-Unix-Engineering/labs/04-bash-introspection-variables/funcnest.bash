#!/usr/bin/env bash
# funcnest.bash: FUNCNEST=3 refuses the fourth nested call, and the script carries on.
# A manual cap at depth 50 stands in where FUNCNEST does not exist (bash before 4.2).
FUNCNEST=3
depth=0
f() {
  depth=$((depth + 1))
  if [ "$depth" -ge 50 ]; then
    echo "no limit: reached depth $depth"
    return 0
  fi
  f
}
f
echo "after: status=$? depth=$depth"
