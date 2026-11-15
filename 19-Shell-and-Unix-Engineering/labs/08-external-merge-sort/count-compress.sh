#!/bin/sh
# count-compress.sh: a pass-through --compress-program that logs every call.
# sort runs it once per temporary file it writes ("write") and once per
# temporary file it reads back ("read", called with -d). Data passes unchanged.
if [ "${1-}" = -d ]; then
  echo read >>"$COUNT_LOG"
else
  echo write >>"$COUNT_LOG"
fi
exec cat
