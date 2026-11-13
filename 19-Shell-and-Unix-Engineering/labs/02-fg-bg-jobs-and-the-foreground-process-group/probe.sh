#!/bin/sh
# probe.sh MODE: give the shell named by LAB_SHELL a fresh pseudo-terminal as its controlling
# terminal and play the user: fgpg types each command line at the shell's prompt, and the SUSP
# character at a running foreground job.
# Needs FGPG (the compiled fgpg.c) and LOGDIR (an empty scratch directory) in the environment.
set -u
mode=${1:?usage: probe.sh MODE}
: "${FGPG:?}" "${LOGDIR:?}"
here=$(dirname "$0")
shell=${LAB_SHELL:-sh}
# fg, bg and the job messages are specified for an interactive shell (POSIX.1-2024 XCU 2.11), so the
# shell gets -i, plus whatever keeps it from reading startup files; fgpg also points HOME at the
# scratch directory and ENV at /dev/null, and sets the prompt it waits for.
if [ -n "${BASH_VERSION-}" ]; then
  interactive=" --norc -i"
elif [ -n "${ZSH_VERSION-}" ]; then
  interactive=" -f -i"
elif [ -n "${YASH_VERSION-}" ]; then
  interactive=" --norcfile -i"
else
  interactive=" -i"
fi

# session STEP...: type `. ./s`, then the steps, then `exit`, at an interactive shell.
# The second `exit` is typed only if the shell prints another prompt, which it does when it
# refuses the first one because a job is still listed.
session() {
  cp "$here/session.sh" "$LOGDIR/s"
  exec "$FGPG" pty "$LOGDIR" --type '=. ./s' "$@" --type '=exit' --type '=exit' \
    --shell "$shell$interactive" --
}

case $mode in
  fg_running)
    # shellcheck disable=SC2016 # the shell on the terminal expands these, not this script
    session --type 'defs.ready="$FGPG" job "$LOGDIR" a bg 7 &' \
      --type 'a.ready=fg >fg.out 2>fg.err' --type '=after_fg a'
    ;;
  suspend)
    # `(exit 5)` first, so a shell that leaves $? alone when a job is suspended shows 5, not 0.
    # shellcheck disable=SC2016 # as above
    session --type 'defs.ready=(exit 5)' --type '="$FGPG" job "$LOGDIR" a fg 7' --key 'a.ready=\032' \
      --type '=after_stop a' --type '=bg >bg.out 2>bg.err' --type '=after_bg' \
      --type '=fg %% >fg.out 2>fg.err' --type '=after_fg a'
    ;;
  ttymode)
    # shellcheck disable=SC2016 # as above
    session --type 'defs.ready="$FGPG" ttymode "$LOGDIR" t' --key 't.ready=\032' \
      --type '=after_stop t' --type '=fg >fg.out 2>fg.err' --type '=after_fg t'
    ;;
  markers)
    # shellcheck disable=SC2016 # as above
    session --type 'defs.ready="$FGPG" hold "$LOGDIR" one &' --type '="$FGPG" hold "$LOGDIR" two &' \
      --type '="$FGPG" hold "$LOGDIR" three &' --type '=markers_check'
    ;;
  jobids)
    session --type 'defs.ready=alpha &' --type '=bravo &' --type '=jobids_check'
    ;;
  pipeline)
    # shellcheck disable=SC2016 # as above
    session --type 'defs.ready="$FGPG" hold "$LOGDIR" first | "$FGPG" hold "$LOGDIR" last &' \
      --type '=pipeline_check'
    ;;
  script_fg)
    exec "$FGPG" pty "$LOGDIR" --shell "$shell" -- "$here/script.sh" fg
    ;;
  script_fg_stderr)
    exec "$FGPG" pty "$LOGDIR" --shell "$shell" -- "$here/script.sh" fg_stderr
    ;;
  *)
    echo "probe.sh: unknown mode $mode" >&2
    exit 2
    ;;
esac
