#!/bin/sh
# characterize.sh - run a script in a sandbox and print a normalized transcript of what it did.
# Usage: characterize.sh [-f FIXTURE_DIR] [-e NAME=VALUE]... [-n SEDFILE] SCRIPT [ARG...]
#   -f DIR   copy the contents of DIR into the sandbox working directory first
#   -e N=V   add one environment variable to the script's environment
#   -n FILE  extra sed script applied to the whole transcript (pids, dates, shell diagnostics)
# Environment:
#   CHAR_ROOT   directory under which the sandbox is created (required, must exist)
#   CHAR_SHELL  shell that runs SCRIPT, words separated by single blanks (default: sh)
# The script runs with `env -i`: only PATH, HOME, TMPDIR, LC_ALL, TZ and CHAR_CALLS (plus every -e)
# are set, the working directory is sandbox/work, stdin is /dev/null, and PATH starts with stub
# `date` and `mail`. The transcript has five sections: status, stdout, stderr, files (every regular
# file left under work, tmp and home, with its contents), and calls (what the stub `mail` recorded).

fixture=
extras=
norm=/dev/null
while getopts f:e:n: opt; do
  case $opt in
    f) fixture=$OPTARG ;;
    e)
      extras="$extras$OPTARG
"
      ;;
    n) norm=$OPTARG ;;
    *)
      echo "usage: characterize.sh [-f DIR] [-e N=V]... [-n SEDFILE] SCRIPT [ARG...]" >&2
      exit 64
      ;;
  esac
done
shift $((OPTIND - 1))
[ "$#" -ge 1 ] || {
  echo "characterize.sh: missing SCRIPT" >&2
  exit 64
}
[ -d "${CHAR_ROOT-}" ] || {
  echo "characterize.sh: CHAR_ROOT must name a directory" >&2
  exit 64
}

sb=$(mktemp -d "$CHAR_ROOT/sandbox.XXXXXX") || exit 70
mkdir "$sb/bin" "$sb/work" "$sb/tmp" "$sb/home"
sbp=$(cd "$sb" && pwd -P)
printf '%s' "$extras" >"$sb/extras"

# Stub date: a fixed instant, whatever the arguments.
printf '#!/bin/sh\necho 2026-01-02 03:04:05\n' >"$sb/bin/date"
# Stub mail: record the arguments and the standard input, never deliver anything.
cat >"$sb/bin/mail" <<'STUB'
#!/bin/sh
{
  printf 'mail'
  printf ' <%s>' "$@"
  printf '\n'
  sed 's/^/  | /'
} >>"$CHAR_CALLS"
STUB
chmod +x "$sb/bin/date" "$sb/bin/mail"

if [ -n "$fixture" ]; then
  cp -R "$fixture"/. "$sb/work/" || exit 70
fi

script=$1
shift
(
  cd "$sb/work" || exit 70
  # Build the command in the positional parameters, so that nothing is field split (native zsh
  # does not split an unquoted expansion): the script and its arguments first, then the words of
  # CHAR_SHELL appended and rotated in front of them, then every -e assignment in front of those.
  set -- "$script" "$@"
  n=$#
  words=${CHAR_SHELL:-sh}
  while [ -n "$words" ]; do
    set -- "$@" "${words%% *}"
    case $words in
      *" "*) words=${words#* } ;;
      *) words= ;;
    esac
  done
  i=0
  while [ "$i" -lt "$n" ]; do
    set -- "$@" "$1"
    shift
    i=$((i + 1))
  done
  while IFS= read -r kv; do
    set -- "$kv" "$@"
  done <"$sb/extras"
  exec env -i PATH="$sb/bin:/usr/bin:/bin" HOME="$sb/home" TMPDIR="$sb/tmp" LC_ALL=C TZ=UTC \
    CHAR_CALLS="$sb/calls" "$@"
) >"$sb/stdout" 2>"$sb/stderr" </dev/null
rc=$?

{
  printf 'status: %s\n' "$rc"
  printf '%s\n' '--- stdout'
  cat "$sb/stdout"
  printf '%s\n' '--- stderr'
  cat "$sb/stderr"
  printf '%s\n' '--- files'
  (cd "$sb" && find work tmp home -type f | LC_ALL=C sort) | while IFS= read -r f; do
    printf '== %s\n' "$f"
    cat "$sb/$f"
  done
  printf '%s\n' '--- calls'
  if [ -f "$sb/calls" ]; then
    cat "$sb/calls"
  fi
} >"$sb/raw"

sed -e "s|$sbp|<SANDBOX>|g" -e "s|$sb|<SANDBOX>|g" "$sb/raw" | sed -f "$norm"
