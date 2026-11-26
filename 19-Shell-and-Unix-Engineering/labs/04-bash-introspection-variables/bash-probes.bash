# shellcheck shell=bash
# shellcheck disable=SC2154 # here is set by bash-probe.sh before it sources this file
# bash-probes.bash: bash-only probes, sourced by bash-probe.sh after its not-bash guard.
# Each probe prints results the .bats file asserts exactly, per bash version.
# Child bashes are started as "$BASH", the full path of the bash under test.

# Scripts that need a fresh bash, so their stack starts at main.
probe_stack() { "$BASH" "$here/stack.bash"; }
probe_demo() { "$BASH" "$here/demo.bash" 2>&1; }
probe_funcnest() {
  # shellcheck disable=SC2312 # funcnest.bash exits 0 either way; sed only shortens the path
  "$BASH" "$here/funcnest.bash" 2>&1 | sed 's#^.*/funcnest.bash: #funcnest.bash: #'
}

# subshell: BASH_SUBSHELL, and whether BASHPID differs from $$, in each kind of subshell.
probe_subshell() {
  report() {
    local fork
    if [ -z "${BASHPID+x}" ]; then
      fork=nopid
    elif [ "$2" = "$$" ]; then
      fork=no
    else
      fork=yes
    fi
    echo "$1 sub=$BASH_SUBSHELL fork=$fork"
  }
  report top "$BASHPID"
  (report paren "$BASHPID")
  ( (report nested "$BASHPID") )
  local line
  line=$(report comsub "$BASHPID")
  echo "$line"
  # shellcheck disable=SC2312 # the pipeline stages are what this probe measures
  { report pipe-left "$BASHPID"; } | cat
  # shellcheck disable=SC2312 # the pipeline stages are what this probe measures
  : | { report pipe-right "$BASHPID"; }
  report async "$BASHPID" &
  wait
  # Process substitution is a parse error in bash before 5.1 in POSIX mode, so it hides in eval.
  if (eval 'cat <(:)') >/dev/null 2>&1; then
    eval 'cat <(report procsub "$BASHPID")'
  else
    echo "procsub unavailable"
  fi
  { report brace "$BASHPID"; }
}

# argv: BASH_ARGV and BASH_ARGC at the top level, in a function, and in a function under extdebug.
probe_argv() {
  # shellcheck disable=SC2016 # the expansions belong to the child bash
  "$BASH" -c 'echo "top: ${BASH_ARGV[*]}|${BASH_ARGC[*]}"
f() { echo "f: ${BASH_ARGV[*]}|${BASH_ARGC[*]}"; }
f x y
shopt -s extdebug
f x y' name a b c
}

# argv0: BASH_ARGV0 sets $0 while special, and stops doing so once unset.
probe_argv0() {
  # shellcheck disable=SC2016 # the expansions belong to the child bash
  "$BASH" -c 'BASH_ARGV0=renamed; echo "after assignment: $0"
unset BASH_ARGV0; BASH_ARGV0=again; echo "after unset: $0"' orig
}

# epoch: the format of EPOCHREALTIME in the C locale, EPOCHSECONDS against it, BASH_MONOSECONDS.
probe_epoch() {
  local LC_ALL=C r s m frac
  r=${EPOCHREALTIME-}
  s=${EPOCHSECONDS-}
  m=${BASH_MONOSECONDS-}
  if [ -z "$r" ]; then
    echo "EPOCHREALTIME absent"
  else
    frac=${r#*.}
    case $r in
      *[!0-9.]* | .* | *.) echo "EPOCHREALTIME malformed: $r" ;;
      *.*) if [ "${#frac}" -eq 6 ]; then echo "EPOCHREALTIME seconds.micro"; else echo "EPOCHREALTIME bad fraction: $r"; fi ;;
      *) echo "EPOCHREALTIME has no point: $r" ;;
    esac
  fi
  if [ -z "$s" ]; then
    echo "EPOCHSECONDS absent"
  elif [ "$s" -ge "${r%.*}" ] && [ "$s" -le $((${r%.*} + 1)) ]; then
    echo "EPOCHSECONDS agrees"
  else
    echo "EPOCHSECONDS disagrees: $s vs $r"
  fi
  case $m in
    '') echo "BASH_MONOSECONDS absent" ;;
    *[!0-9]*) echo "BASH_MONOSECONDS not an integer: $m" ;;
    *) echo "BASH_MONOSECONDS integer seconds" ;;
  esac
}

# srandom: SRANDOM stays in 0..4294967295 and ignores assignment.
probe_srandom() {
  local a b
  if [ -z "${SRANDOM+x}" ]; then
    echo "SRANDOM absent"
    return 0
  fi
  SRANDOM=5
  a=$SRANDOM
  b=$SRANDOM
  if [ "$a" -ge 0 ] && [ "$a" -le 4294967295 ] && [ "$b" -ge 0 ] && [ "$b" -le 4294967295 ]; then
    echo "SRANDOM in range"
  fi
  if [ "$a" != 5 ] && [ "$a" != "$b" ]; then echo "SRANDOM ignores assignment"; fi
}

# random: seeding RANDOM repeats its sequence, but a command substitution reseeds itself.
probe_random() {
  local a b child parent
  RANDOM=42
  a="$RANDOM $RANDOM $RANDOM"
  RANDOM=42
  b="$RANDOM $RANDOM $RANDOM"
  if [ "$a" = "$b" ]; then echo "seeded sequence repeats"; else echo "seeded sequence differs"; fi
  RANDOM=42
  # shellcheck disable=SC2116 # the command substitution is the subshell under test
  child=$(echo "$RANDOM $RANDOM $RANDOM")
  parent="$RANDOM $RANDOM $RANDOM"
  if [ "$child" = "$parent" ]; then echo "subshell continues the sequence"; else echo "subshell reseeds"; fi
}

# shlvl: SHLVL across nested bashes, exec, and the 1000 ceiling.
probe_shlvl() {
  # shellcheck disable=SC2016 # the expansions belong to the child bashes
  SHLVL=1 "$BASH" -c '"$BASH" -c "echo nested-last=\$SHLVL"'
  # shellcheck disable=SC2016 # the expansions belong to the child bashes
  SHLVL=1 "$BASH" -c '"$BASH" -c "echo nested-not-last=\$SHLVL"; :'
  # shellcheck disable=SC2016 # the expansions belong to the child bashes
  SHLVL=1 "$BASH" -c 'exec "$BASH" -c "echo exec=\$SHLVL"'
  # bash 3.2 to 4.3 leave SHLVL=1000 unformatted (stack garbage), so classify it, never print it.
  # shellcheck disable=SC2016 # the expansion belongs to the child bash
  SHLVL=999 "$BASH" -c 'case $SHLVL in
  1) echo "from-999: reset to 1" ;;
  1000) echo "from-999: 1000" ;;
  "" | *[!0-9]*) echo "from-999: not a number" ;;
  *) echo "from-999: other number" ;;
esac' 2>/dev/null
}

# trapsig: BASH_TRAPSIG inside a signal trap and inside the EXIT trap.
probe_trapsig() {
  local usr1
  usr1=$(kill -l USR1)
  # shellcheck disable=SC2154 # usr1 is set above; the trap string expands it when it runs
  trap 'if [ -z "${BASH_TRAPSIG+x}" ]; then echo "USR1 trap: BASH_TRAPSIG unset"; elif [ "$BASH_TRAPSIG" = "$usr1" ]; then echo "USR1 trap: BASH_TRAPSIG is the signal number"; else echo "USR1 trap: BASH_TRAPSIG=$BASH_TRAPSIG"; fi' USR1
  kill -USR1 $$
  trap - USR1
  # shellcheck disable=SC2016 # the expansion belongs to the child bash
  "$BASH" -c 'trap '\''echo "EXIT trap: BASH_TRAPSIG=${BASH_TRAPSIG-unset}"'\'' EXIT'
}

# command: BASH_COMMAND outside a trap, inside a signal trap, and inside the EXIT trap.
probe_command() {
  echo "plain: $BASH_COMMAND"
  trap 'echo "in trap: $BASH_COMMAND"' USR1
  kill -USR1 $$
  trap - USR1
  # shellcheck disable=SC2016 # the expansion belongs to the child bash
  "$BASH" -c 'trap '\''echo "EXIT trap: $BASH_COMMAND"'\'' EXIT; true last'
}

# unset: dynamic variables ignore assignment until unset; the stack arrays refuse unset.
probe_unset() {
  local before st
  BASHPID=5
  if [ "$BASHPID" = 5 ]; then before=stored; else before=ignored; fi
  unset BASHPID
  BASHPID=5
  echo "BASHPID=5 before unset: $before; after unset: $BASHPID"
  st=0
  # In POSIX mode a failed special builtin exits the shell.
  (unset BASH_SOURCE) 2>/dev/null || st=$?
  echo "unset BASH_SOURCE: status $st"
}

# cfunc: what the stack variables hold in a function defined by bash -c.
probe_cfunc() {
  # shellcheck disable=SC2016 # the expansions belong to the child bash
  "$BASH" -c 'f() { echo "BASH_SOURCE=<${BASH_SOURCE[*]}> count=${#BASH_SOURCE[@]}"; caller >/dev/null; echo "caller status $?"; }; f' myname
}

# envfunc: what BASH_SOURCE holds in a function imported from the environment.
probe_envfunc() {
  # shellcheck disable=SC2329 # the child bash calls f after importing it from the environment
  f() { echo "BASH_SOURCE=<${BASH_SOURCE[*]}> count=${#BASH_SOURCE[@]}"; }
  export -f f
  "$BASH" -c f
}

# guard: the BASH_SOURCE and return tests for "am I sourced?" under three ways of running a file.
probe_guard() {
  local g
  for g in guard.bash guard-return.bash; do
    "$BASH" "$here/$g"
    # shellcheck disable=SC2016 # the expansion belongs to the child bash
    "$BASH" -c '. "$1"' sh "$here/$g"
    "$BASH" <"$here/$g"
  done
}

# fullpath: bash 5.3 shopt bash_source_fullpath makes BASH_SOURCE entries absolute.
probe_fullpath() {
  (
    cd "$here" || exit 1
    "$BASH" -c 'shopt -s bash_source_fullpath 2>/dev/null || { echo "bash_source_fullpath absent"; exit 0; }; . ./srcpath.bash'
    "$BASH" -c '. ./srcpath.bash'
  )
}

# versinfo: BASH_VERSINFO agrees with BASH_VERSION and is readonly.
probe_versinfo() {
  local v c ro
  v="${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}.${BASH_VERSINFO[2]}"
  case $BASH_VERSION in
    "$v"*) c=yes ;;
    *) c=no ;;
  esac
  if (BASH_VERSINFO[0]=99) 2>/dev/null; then ro=no; else ro=yes; fi
  echo "BASH_VERSINFO matches BASH_VERSION: $c; readonly: $ro"
}

# execstring: BASH_EXECUTION_STRING is the -c argument, verbatim.
probe_execstring() {
  # shellcheck disable=SC2016 # the expansion belongs to the child bash
  "$BASH" -c 'echo "$BASH_EXECUTION_STRING"'
}

case ${1-} in
  stack | demo | funcnest | subshell | argv | argv0 | epoch | srandom | random | shlvl | trapsig | command | unset | cfunc | envfunc | guard | fullpath | versinfo | execstring)
    "probe_$1"
    ;;
  *)
    echo "usage: bash-probe.sh MODE (see bash-probes.bash)" >&2
    exit 2
    ;;
esac
