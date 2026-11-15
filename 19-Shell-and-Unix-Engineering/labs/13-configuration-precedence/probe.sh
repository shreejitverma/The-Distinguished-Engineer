#!/bin/sh
# probe.sh MODE DIR: show what the shell under test does when a script loads a config file
# with the dot special built-in. DIR is a scratch directory the test prepared; the probe
# only reads it and never removes anything.
#   dot-missing     . ./missing.conf, a file that does not exist, then report survival
#   dot-cwd         . demo-cwd-only.conf, a slash-free name present only in DIR, with PATH
#                   pointing at an empty directory, then report what loaded
#   dot-late-error  . ./late-error.conf, whose line 1 runs and line 3 is a syntax error
#   naive-source    load ./naive.conf (port=9000) after reading DEMO_PORT: the file wins
#   guarded-source  load ./guarded.conf (: "${port:=9000}") after DEMO_PORT: the env wins
# Exit status: the status of the last command, or the shell's own status if it aborts.

mode=${1:?usage: probe.sh MODE DIR}
dir=${2:?usage: probe.sh MODE DIR}
cd "$dir" || exit 1

case $mode in
  dot-missing)
    # shellcheck source=/dev/null # the file deliberately does not exist
    . ./missing.conf
    printf 'survived status=%s\n' "$?"
    ;;
  dot-cwd)
    # The file only assigns cwd_conf, and PATH is restored before anything is printed,
    # because yash runs a regular built-in such as echo only if PATH also finds it.
    saved_path=$PATH
    # shellcheck disable=SC2123 # replacing PATH is the point: dot must not find the file there
    PATH=$dir/empty-bin
    # shellcheck source=/dev/null # the file is created by the test
    . demo-cwd-only.conf
    st=$?
    PATH=$saved_path
    case ${cwd_conf-} in
      '') ;;
      *) echo "$cwd_conf" ;;
    esac
    echo "after status=$st"
    ;;
  dot-late-error)
    # shellcheck source=/dev/null # the file is created by the test
    . ./late-error.conf
    printf 'after status=%s\n' "$?"
    ;;
  naive-source)
    port=${DEMO_PORT:-8080}
    # shellcheck source=/dev/null # the file is created by the test
    . ./naive.conf
    printf 'naive port=%s\n' "$port"
    ;;
  guarded-source)
    port=${DEMO_PORT-}
    # shellcheck source=/dev/null # the file is created by the test
    . ./guarded.conf
    : "${port:=8080}"
    printf 'guarded port=%s\n' "$port"
    ;;
  *)
    printf 'probe.sh: unknown mode %s\n' "$mode" >&2
    exit 2
    ;;
esac
