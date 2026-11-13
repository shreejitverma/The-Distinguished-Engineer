#!/bin/sh
# shquote.sh: print one line that the running shell parses back into exactly the given arguments.
# It uses printf -v with %q when the shell really has both (bash 3.1+, zsh 5.3+, ksh93u+m),
# detected by effect, and otherwise a fork-free POSIX single-quote fallback.
# Usage: shquote.sh [ARG...]
# Example: shquote.sh 'a b' "it's"  ->  a\ b it\'s          (bash, zsh)
#          shquote.sh 'a b' "it's"  ->  'a b' 'it'\''s'     (POSIX fallback)
# The %q dialect belongs to the running shell: feed the line back only to that same shell.

# sq_append WORD: append WORD, single-quoted so that any POSIX shell reads it back, and a space, to out.
sq_append() {
  sq_rest=$1
  sq_done=
  while :; do
    case $sq_rest in
      *\'*)
        sq_done=$sq_done${sq_rest%%\'*}\'\\\'\'
        sq_rest=${sq_rest#*\'}
        ;;
      *) break ;;
    esac
  done
  out="$out'$sq_done$sq_rest' "
}

# Detect printf -v by its effect, not its exit status: BusyBox ash and GNU printf take -v as the
# format, print it, and exit 0. The probe runs in a subshell because zsh aborts on a -v error.
out=
# shellcheck disable=SC3045 # the probe exists to find out whether printf -v works here
if (
  probe=
  printf -v probe %q ok >/dev/null 2>&1 && [ "$probe" = ok ]
) 2>/dev/null; then
  # bash turns a %q with no argument into '', so an empty argument list must not reach printf.
  if [ "$#" -gt 0 ]; then
    # shellcheck disable=SC3045 # printf -v runs only after the probe above proved the shell has it
    printf -v out '%q ' "$@"
  fi
else
  for arg in "$@"; do
    sq_append "$arg"
  done
fi
printf '%s\n' "${out% }"
