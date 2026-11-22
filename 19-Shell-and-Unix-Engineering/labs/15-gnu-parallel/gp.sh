#!/bin/sh
# gp.sh: run GNU parallel with the citation notice silenced and, unless GP_AUTO_SHELL is set,
# with the job shell pinned to /bin/sh so a lab result does not depend on which shell started it.
# With GP_AUTO_SHELL non-empty, PARALLEL_SHELL is unset and parallel must find the shell itself
# by walking up its own ancestors (parallel_design: "Which shell to use"), so it is not exec'd:
# its parent has to stay a shell for that search to find one.
# Usage: gp.sh PARALLEL_ARGS...
if [ -n "${GP_AUTO_SHELL-}" ]; then
  unset PARALLEL_SHELL
  parallel --will-cite "$@"
else
  PARALLEL_SHELL=/bin/sh
  export PARALLEL_SHELL
  exec parallel --will-cite "$@"
fi
