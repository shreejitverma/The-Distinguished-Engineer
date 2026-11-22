#!/bin/sh
# probe.sh: examine BASH_ENV and ENV execution, expansion, pathing, and privilege behavior across shells.
# Usage: probe.sh MODE SHELL [SHELL_ARG...]
# Output:
#   trace: TAG...    files or expansions that ran in startup, in order ("-" if none)
#   body: OUTPUT     what the shell body printed ("-" if none)
#   status: N        the shell exit status

set -eu

usage() {
  printf 'Usage: %s MODE SHELL [SHELL_ARG...]\n' "$0" >&2
  exit 2
}

die() {
  printf 'probe.sh: %s\n' "$*" >&2
  exit 2
}

[ "$#" -ge 2 ] || usage

mode=$1
shift
case $mode in
  noninteractive | interactive | posix-interactive | posix-noninteractive | cmdsub | cmdsub-env | errexit | cwd | privileged | restricted | params) ;;
  *) die "unknown mode '$mode'" ;;
esac

[ -n "${TMPDIR-}" ] || die "TMPDIR must be set to a scratch directory"

shell=$(command -v "$1") || die "shell not found: $1"
shift

sandbox=$(mktemp -d "$TMPDIR/envprobe.XXXXXX") || die "mktemp failed"
cleanup() {
  rm -rf "$sandbox"
}
trap cleanup EXIT INT TERM

trace=$sandbox/trace
out=$sandbox/out
err=$sandbox/err
home=$sandbox/home
mkdir -p "$home"
: >"$trace"
: >"$out"
: >"$err"

bash_env_file=$sandbox/bash_env.sh
env_file=$sandbox/env.sh

printf "printf '%%s\\n' BASH_ENV >> '%s'\n" "$trace" >"$bash_env_file"
printf "printf '%%s\\n' ENV >> '%s'\n" "$trace" >"$env_file"

bash_env=$bash_env_file
posix_env=$env_file

exit_code=0

case $mode in
  noninteractive)
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$bash_env" "ENV=$posix_env" \
        "$shell" "$@" -c 'printf "body: %s\n" main_ran'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  interactive)
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$bash_env" "ENV=$posix_env" \
        "$shell" "$@" -i -c 'printf "body: %s\n" main_ran'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  posix-interactive)
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$bash_env" "ENV=$posix_env" POSIXLY_CORRECT=1 \
        "$shell" "$@" -i -c 'printf "body: %s\n" main_ran'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  posix-noninteractive)
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$bash_env" "ENV=$posix_env" POSIXLY_CORRECT=1 \
        "$shell" "$@" -c 'printf "body: %s\n" main_ran'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  cmdsub)
    # shellcheck disable=SC2016 # the command substitution is for the shell under test to expand
    cmdsub_val='$(printf "CMDSUB\n" >> "'"$trace"'")'
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$cmdsub_val" "ENV=$posix_env" \
        "$shell" "$@" -c 'printf "body: %s\n" main_ran'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  cmdsub-env)
    # shellcheck disable=SC2016 # the command substitution is for the shell under test to expand
    cmdsub_val='$(printf "CMDSUB_ENV\n" >> "'"$trace"'")'
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$bash_env" "ENV=$cmdsub_val" \
        "$shell" "$@" -i -c 'printf "body: %s\n" main_ran'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  errexit)
    cat <<EOF >"$bash_env_file"
false
printf 'ENV_SURVIVED\n' >> '$trace'
EOF
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$bash_env" "ENV=$posix_env" \
        "$shell" "$@" -e -c 'printf "body: before_false\n"; false; printf "body: after_false\n"'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  cwd)
    workdir=$sandbox/workdir
    bindir=$sandbox/bindir
    mkdir -p "$workdir" "$bindir"
    printf "printf '%%s\\n' CWD_INIT >> '%s'\n" "$trace" >"$workdir/init.sh"
    printf "printf '%%s\\n' PATH_INIT >> '%s'\n" "$trace" >"$bindir/init.sh"
    chmod +x "$workdir/init.sh" "$bindir/init.sh"
    (
      cd "$workdir"
      env -i "PATH=$bindir:$PATH" "HOME=$home" "BASH_ENV=init.sh" "ENV=init.sh" \
        "$shell" "$@" -c 'printf "body: %s\n" main_ran'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  privileged)
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$bash_env" "ENV=$posix_env" \
        "$shell" "$@" -p -c 'printf "body: %s\n" main_ran'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  restricted)
    cat <<EOF >"$bash_env_file"
/bin/echo "RESTRICTED_BASH_ENV" >> "$trace"
EOF
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$bash_env" "ENV=$posix_env" \
        "$shell" "$@" -r -c 'printf "body: %s\n" main_ran'
    ) >"$out" 2>"$err" || exit_code=$?
    ;;

  params)
    cat <<EOF >"$bash_env_file"
printf 'params: 0=%s #=%d\n' "\$0" "\$#" >> "$trace"
EOF
    cat <<'EOF' >"$sandbox/script.sh"
printf 'body: script 0=%s #=%d\n' "$0" "$#"
EOF
    chmod +x "$sandbox/script.sh"
    (
      env -i "PATH=$PATH" "HOME=$home" "BASH_ENV=$bash_env" "ENV=$posix_env" \
        "$shell" "$@" "$sandbox/script.sh" first second
    ) >"$out" 2>"$err" || exit_code=$?
    ;;
esac

# Collect trace line
if [ -s "$trace" ]; then
  tracestr=$(tr '\n' ' ' <"$trace" | sed 's/ $//')
  printf 'trace: %s\n' "$tracestr"
else
  printf 'trace: -\n'
fi

# Collect body output
bodystr=$(grep '^body: ' "$out" 2>/dev/null | sed 's/^body: //' | tr '\n' ' ' | sed 's/ $//' || true)
if [ -n "$bodystr" ]; then
  printf 'body: %s\n' "$bodystr"
else
  printf 'body: -\n'
fi

printf 'status: %d\n' "$exit_code"
