#!/bin/sh
# shellcheck disable=SC3043,SC3044 # probing the non-POSIX local, declare and shopt is the point of this script
# scope_probe.sh MODE: measure how `local` and `unset` interact under dynamic scope.
#   unset-caller    a callee unsets its caller's local: hidden again, or the global exposed?
#   unset-own       a function unsets its own local, then assigns: local or global?
#   upvar           a callee returns a value by unset + eval into its caller's local
#   export-local    a local shadows an exported global: what does a child process see?
#   readonly-local  a local declared over a readonly global
#   declare-g       declare -g in a callee whose caller has a local of the same name
#   localvar-unset  unset-caller again with bash's localvar_unset option set
#   inherit         a fresh local, then local -I and the localvar_inherit option (bash)
#   valsub          ${| cmd; } (bash 5.3, mksh): a value returned through a local REPLY, no fork
# Shells without local (ksh93) report their error and run the rest with globals.

case ${1-} in
  unset-caller)
    x=global
    outer() {
      local x=outer_local
      inner
      printf 'outer_after_inner=%s\n' "${x-UNSET}"
    }
    inner() {
      unset x
      printf 'inner_after_unset=%s\n' "${x-UNSET}"
    }
    outer
    printf 'top=%s\n' "${x-UNSET}"
    ;;
  unset-own)
    x=global
    f() {
      local x=local
      unset x
      printf 'after_unset=%s\n' "${x-UNSET}"
      x=again
    }
    f
    printf 'top=%s\n' "${x-UNSET}"
    ;;
  upvar)
    f() {
      local result
      g result
      printf 'f_result=%s\n' "${result-UNSET}"
    }
    g() {
      # shellcheck disable=SC2034 # tmp is read by the eval below
      local tmp=computed
      unset -v "$1"
      eval "$1=\$tmp"
    }
    f
    printf 'top_result=%s\n' "${result-UNSET}"
    ;;
  export-local)
    E=exported
    export E
    f() {
      local E
      E=newval
      child=$(env | grep '^E=')
      printf 'child_sees=%s\n' "${child:-ABSENT}"
    }
    f
    ;;
  readonly-local)
    readonly ro=frozen
    f() {
      local ro=thawed
      printf 'local_status=%s inside=%s\n' "$?" "$ro"
    }
    f
    printf 'top=%s\n' "$ro"
    ;;
  declare-g)
    x=global
    writer() {
      declare -g x=from_g
      printf 'writer_sees=%s\n' "$x"
    }
    call_writer() {
      local x=caller_local
      writer
      printf 'caller_sees=%s\n' "$x"
    }
    call_writer
    printf 'top=%s\n' "$x"
    ;;
  localvar-unset)
    if ! shopt -s localvar_unset 2>/dev/null; then
      printf 'localvar_unset=unsupported\n'
      exit 3
    fi
    x=global
    outer() {
      local x=outer_local
      inner
      printf 'outer_after_inner=%s\n' "${x-UNSET}"
    }
    inner() {
      unset x
      printf 'inner_after_unset=%s\n' "${x-UNSET}"
    }
    outer
    ;;
  inherit)
    v=outer_value
    with_I() {
      local -I v
      printf 'local_I=%s\n' "${v-UNSET}"
    }
    plain() {
      local v
      printf 'plain=%s\n' "${v-UNSET}"
    }
    plain
    with_I
    if shopt -s localvar_inherit 2>/dev/null; then
      plain
    else
      printf 'localvar_inherit=unsupported\n'
    fi
    ;;
  valsub)
    # bash 5.3 and mksh: ${| cmd; } runs cmd in this shell and expands to its local REPLY.
    # The probe runs in a subshell because a syntax error inside eval may end the whole shell;
    # n proves that f ran in the same shell that expanded the substitution.
    REPLY=outer
    if out=$( (eval 'n=0; f() { n=$((n + 1)); REPLY=fn_result; }; x=${| f; }; printf "%s %s %s" "$x" "$n" "$REPLY"') 2>/dev/null); then
      printf 'valsub=%s\n' "$out"
    else
      printf 'valsub=unsupported\n'
    fi
    ;;
  *)
    printf 'usage: scope_probe.sh MODE (see the header)\n' >&2
    exit 64
    ;;
esac
