#!/bin/sh
# lineno.sh: print LINENO at the top level (file line 9) and in a function body (file line 7).
# Shells disagree on the second number: the file line, or a count from the function.
# The test asserts exact numbers, so keep this layout unchanged.
f() {
  :
  echo "top=$top func=$LINENO"
}
top=$LINENO
f
