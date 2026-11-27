#!/bin/sh
# cell-facts.sh: print facts about the shell that runs this script, one key=value per line.
# Every value is computed here, by the shell under test, never looked up by name.
# Usage: SHELL cell-facts.sh
#   ifs_hex         the bytes of IFS when the script starts (POSIX.1-2024 XCU 2.5.3: the shell sets
#                   IFS to space, tab, newline when it is invoked, so 20090a)
#   split_count     how many fields an unquoted expansion of "a  b" yields (2; native zsh gives 1)
#   dollar_squote   what a dollar-single-quote word with a hex escape expands to (A, or $\x41)
#   sigkill_status  the status of a child killed by SIGKILL (128+9, 256+9 or 384+9, by shell)
#   pipefail        no, or yes/rc=N where N is the status of a pipeline "false | true" under pipefail

# shellcheck disable=SC3040 # pipefail is probed on purpose: a shell that lacks it fails the probing subshell

# Capture IFS before anything assigns it.
ifs_hex=$(printf '%s' "$IFS" | od -An -tx1 | tr -d ' \n')

IFS=' '
x='a  b'
# shellcheck disable=SC2086,SC2248 # the unquoted expansion is the probe
set -- $x
split_count=$#

# The dollar-single-quote word sits in a string given to eval, so shells that cannot parse it
# (or a formatter) never see it as source text.
code="dq=\$'\\x41'"
eval "$code"
# shellcheck disable=SC2154 # dq is assigned by the eval above
dollar_squote=$dq

# The child is /bin/sh, an external process, so the status is the outer shell's view of a signal death.
sigkill_status=$({
  /bin/sh -c 'kill -s KILL $$'
  printf '%s' "$?"
} 2>/dev/null)

pipefail=no
if (set -o pipefail) 2>/dev/null; then
  (
    set -o pipefail
    false | true
  )
  pipefail=yes/rc=$?
fi

printf 'ifs_hex=%s\n' "$ifs_hex"
printf 'split_count=%s\n' "$split_count"
printf 'dollar_squote=%s\n' "$dollar_squote"
printf 'sigkill_status=%s\n' "$sigkill_status"
printf 'pipefail=%s\n' "$pipefail"
