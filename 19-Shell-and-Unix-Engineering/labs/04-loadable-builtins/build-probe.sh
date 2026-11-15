#!/bin/sh
# build-probe.sh: compile probe.c into OUTDIR/probe.so, plus a copy named OUTDIR/probe
# (bash 5.2+ `enable probe` looks for a file named exactly like the builtin).
# Usage: build-probe.sh OUTDIR
# -shared works with GCC on Linux and with Apple clang, which treats it as -dynamiclib.
set -eu
out=${1:?usage: build-probe.sh OUTDIR}
here=$(cd "$(dirname "$0")" && pwd)
"${CC:-cc}" -Wall -Wextra -Werror -O2 -shared -fPIC -o "$out/probe.so" "$here/probe.c"
cp "$out/probe.so" "$out/probe"
