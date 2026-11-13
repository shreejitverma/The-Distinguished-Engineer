#!/bin/sh
# startup-probe: start a shell in a sandbox HOME in which every startup file records that it ran,
# then report which files ran, what the command saw, and how the shell exited.
# Usage: startup-probe.sh MODE SHELL [SHELL_ARG...]
#   TMPDIR must name a scratch directory; the sandbox is a mktemp -d directory inside it.
# Output, three lines:
#   trace: TAG...    the startup files that ran, in order ("-" when none ran)
#   body: FACT...    what the -c command printed ("-" when it printed nothing)
#   status: N        the shell's exit status
# The shell runs under env -i with PATH, HOME (the sandbox), and an inherited environment that
# bash in particular acts on at startup:
#   ENV and BASH_ENV        each name a startup file, tagged ENV and BASH_ENV
#   SHELLOPTS=noglob        bash imports set -o options from it
#   PS1='probe$ '           bash unsets PS1 in a non-interactive shell
#   BASH_FUNC_probe_fn%%    the post-2014 encoding of an exported bash function
# HOME holds every per-user startup file of bash, zsh, ksh93, mksh, and yash, each tagged with its
# name without the dot (.bash_profile -> bash_profile).
# The default command prints: ran fn=imported|absent noglob=on|off shlvl=N|unset ps1=set|unset
# posix=yes|no (SHELLOPTS contains posix) interactive=yes|no cd=ok|denied (cd / worked).
# Modes:
#   noninteractive     SHELL -c CMD
#   expand             as noninteractive, but BASH_ENV holds a command substitution, not a path
#   args               SHELL -c CMD zero a b; the BASH_ENV file records $0, $#, and $1
#   errexit            SHELL -e -c 'echo ran; false; echo not-reached'; BASH_ENV runs false first
#   interactive        SHELL -i -c CMD
#   login              SHELL -l -c CMD
#   fallback           login, without ~/.bash_profile and ~/.bash_login
#   empty              login, with an empty ~/.bash_profile
#   unreadable         login, with a ~/.bash_profile that cannot be read (mode 000)
#   logout             SHELL -l -c 'CMD; exit 0'
#   exit               SHELL -c 'CMD; exit 0' (not a login shell, so no logout file runs)
#   minus-name         exit, with argv[0] "-NAME" (how login(1) marks a login shell)
#   sh-noninteractive  noninteractive, with the shell started under the name sh
#   sh-interactive     interactive, as sh; ENV records whether posix mode was on while it ran
#   sh-login           login, as sh
#   posix-interactive  interactive, with POSIXLY_CORRECT=1; ENV records posix mode as above
#   posix-login        login, with POSIXLY_CORRECT=1
#   socket             noninteractive, with standard input a connected socket (as under sshd)
#   socket-nested      socket, with SHLVL=1 inherited (the shell is not top level)
#   privileged         noninteractive, with -p
#   restricted         noninteractive, with -r; BASH_ENV runs cd / before restrictions apply

usage() {
  sed -n '3,4s/^# \{0,1\}//p' "$0" >&2
  exit 2
}

die() {
  printf 'startup-probe: %s\n' "$*" >&2
  exit 2
}

[ "$#" -ge 2 ] || usage
mode=$1
shift
case $mode in
  noninteractive | expand | args | errexit | interactive | login | fallback | empty | unreadable) ;;
  logout | exit | sh-noninteractive | sh-interactive | sh-login | posix-interactive | posix-login) ;;
  minus-name | socket | socket-nested | privileged | restricted) ;;
  *) die "unknown mode '$mode'" ;;
esac
[ -n "${TMPDIR-}" ] || die "set TMPDIR to a scratch directory"

here=$(cd "$(dirname "$0")" && pwd -P) || die "cannot find the probe's directory"
shell=$(command -v "$1") || die "shell not found: $1"
shift
sandbox=$(mktemp -d "$TMPDIR/startup.XXXXXX") || die "mktemp failed"
home=$sandbox/home
trace=$sandbox/trace
mkdir "$home" || die "mkdir failed"
: >"$trace"

# tag_file PATH TAG: write a startup file that appends TAG to the trace.
tag_file() {
  printf '%s\n' "printf '%s\\n' $2 >>'$trace'" >"$1"
}

for name in profile bash_profile bash_login bashrc bash_logout zshenv zprofile zshrc zlogin zlogout \
  kshrc mkshrc yashrc yash_profile; do
  tag_file "$home/.$name" "$name"
done
tag_file "$sandbox/env.sh" ENV
tag_file "$sandbox/bash_env.sh" BASH_ENV
bash_env=$sandbox/bash_env.sh

# The command the shell runs; it uses no redirection, which a restricted shell refuses.
# shellcheck disable=SC2016 # the expansions belong to the shell under test
body='fn=absent
probe_fn
set -- /*
case $1 in "/*") g=on ;; *) g=off ;; esac
case $- in *i*) i=yes ;; *) i=no ;; esac
case :${SHELLOPTS-}: in *:posix:*) p=yes ;; *) p=no ;; esac
if [ -n "${PS1+x}" ]; then s=set; else s=unset; fi
if cd /; then c=ok; else c=denied; fi
printf "%s\n" "ran fn=$fn noglob=$g shlvl=${SHLVL-unset} ps1=$s posix=$p interactive=$i cd=$c"'

# posix_tag_file PATH TAG: like tag_file, but the tag says whether SHELLOPTS held posix then.
posix_tag_file() {
  printf '%s\n' "case :\${SHELLOPTS-}: in *:posix:*) p=yes ;; *) p=no ;; esac" \
    "printf '%s\\n' \"$2(posix=\$p)\" >>'$trace'" >"$1"
}

# Shell options, in the order they follow the shell's own arguments.
opts=
case $mode in
  interactive | sh-interactive | posix-interactive) opts=-i ;;
  login | fallback | empty | unreadable | logout | sh-login | posix-login) opts=-l ;;
  errexit) opts=-e ;;
  privileged) opts=-p ;;
  restricted) opts=-r ;;
  *) ;;
esac

case $mode in
  expand)
    # shellcheck disable=SC2016 # the command substitution is for the shell under test to expand
    bash_env='$(printf "%s\n" cmdsub >>'"'$trace'"')'
    ;;
  args)
    printf '%s\n' "printf '%s\\n' \"BASH_ENV(0=\$0,#=\$#,1=\${1-})\" >>'$trace'" >"$bash_env"
    ;;
  errexit)
    printf '%s\n' false "printf '%s\\n' BASH_ENV-after-false >>'$trace'" >"$bash_env"
    body='echo ran
false
echo not-reached'
    ;;
  fallback) rm -f "$home/.bash_profile" "$home/.bash_login" ;;
  empty) : >"$home/.bash_profile" ;;
  unreadable)
    chmod 000 "$home/.bash_profile"
    if [ -r "$home/.bash_profile" ]; then
      rm -rf "$sandbox"
      die "cannot make a file unreadable here (running as root?)"
    fi
    ;;
  logout | exit | minus-name)
    body="$body
exit 0"
    ;;
  sh-interactive | posix-interactive) posix_tag_file "$sandbox/env.sh" ENV ;;
  restricted)
    printf '%s\n' "cd / && printf '%s\\n' BASH_ENV-cd-ok >>'$trace'" >"$bash_env"
    ;;
  *) ;;
esac

# Build the command line: the shell words, its options, -c, the command, and any operands.
# For busybox, the applet word ("ash") goes only when argv[0] itself selects the applet.
set -- "$shell" "$@"
base=${shell##*/}
case $mode in
  sh-*)
    mkdir "$sandbox/bin" || die "mkdir failed"
    ln -s "$shell" "$sandbox/bin/sh" || die "ln failed"
    case $base in
      busybox) shift 2 ;; # the name sh alone selects ash
      *) shift ;;
    esac
    set -- "$sandbox/bin/sh" "$@"
    ;;
  minus-name)
    if [ "$base" = busybox ]; then
      base=$2 # "-ash" selects ash and marks a login shell
      shift 2
      set -- "$shell" "$@"
    fi
    ;;
  *) ;;
esac
if [ -n "$opts" ]; then
  set -- "$@" "$opts"
fi
set -- "$@" -c "$body"
if [ "$mode" = args ]; then
  set -- "$@" zero a b
fi
case $mode in
  minus-name | socket | socket-nested)
    "${CC:-cc}" -o "$sandbox/launch" "$here/launch.c" || die "cannot compile launch.c"
    case $mode in
      minus-name) set -- "$sandbox/launch" -a "-$base" "$@" ;;
      *) set -- "$sandbox/launch" -s "$@" ;;
    esac
    ;;
  *) ;;
esac
case $mode in
  posix-*) set -- POSIXLY_CORRECT=1 "$@" ;;
  socket-nested) set -- SHLVL=1 "$@" ;;
  *) ;;
esac
set -- env -i "PATH=$PATH" "HOME=$home" "ENV=$sandbox/env.sh" "BASH_ENV=$bash_env" \
  SHELLOPTS=noglob 'PS1=probe$ ' 'BASH_FUNC_probe_fn%%=() { fn=imported; }' "$@"

(cd "$home" && exec "$@") >"$sandbox/out" 2>"$sandbox/err" </dev/null
rc=$?

# join FILE: print the lines of FILE on one line, separated by spaces, or "-" when it is empty.
join() {
  joined=
  while IFS= read -r line; do
    joined="$joined${joined:+ }$line"
  done <"$1"
  printf '%s\n' "${joined:--}"
}

traced=$(join "$trace") || traced="?"
printed=$(join "$sandbox/out") || printed="?"
printf 'trace: %s\n' "$traced"
printf 'body: %s\n' "$printed"
printf 'status: %s\n' "$rc"
if [ -n "${STARTUP_PROBE_STDERR-}" ]; then
  sed 's/^/stderr: /' "$sandbox/err"
fi
chmod -R u+rwx "$sandbox"
rm -rf "$sandbox"
