#!/bin/sh
# shellcheck disable=SC3044,SC3045 # probing the non-POSIX typeset -n and unset -n is the point of this script
# nameref_probe.sh MODE [ARG]: measure one property of `typeset -n` in the shell running it.
#   support         print namerefs=yes or namerefs=no (the only mode that runs without namerefs)
#   chain N         build r1 -> target, r2 -> r1, ..., rN -> r(N-1); expand rN
#   bind            a nameref to a[i]: is the subscript fixed at declaration or at each use?
#   name            ${!ref} on a nameref, then plain unset through it
#   unset-n         unset -n removes the reference itself, leaving the target
#   selfref         typeset -n x=x at global scope
#   circular-local  a callee binds `out` to the name `out` while its caller has a local `out`
#   asym            a callee shadows the target: does a read through the nameref reach the same variable as a write?
#   for             a nameref as the control variable of a for loop
#   positional      typeset -n r=1 with $1 set
#   print           typeset -p of a nameref
# Every other mode exits 3 with "namerefs: unsupported" when the shell lacks typeset -n.
# Constructs that some shells cannot even parse (${!x}, the function keyword) are in eval strings.

mode=${1-}
# shellcheck disable=SC2034 # lab_ref exists only to test whether typeset -n is accepted
if (typeset -n lab_ref=lab_target) >/dev/null 2>&1; then
  have=yes
else
  have=no
fi
if [ "$mode" = support ]; then
  printf 'namerefs=%s\n' "$have"
  exit 0
fi
if [ "$have" = no ]; then
  printf 'namerefs: unsupported\n'
  exit 3
fi

case $mode in
  chain)
    target=hit
    prev=target
    i=1
    while [ "$i" -le "$2" ]; do
      eval "typeset -n r$i=\$prev"
      prev=r$i
      i=$((i + 1))
    done
    eval "printf 'depth=%s value=%s\n' \"\$2\" \"\${$prev-UNSET}\""
    ;;
  bind)
    eval 'a=(zero one two)'
    i=0
    typeset -n e='a[i]'
    printf 'i=0 e=%s\n' "$e"
    i=2
    printf 'i=2 e=%s\n' "$e"
    ;;
  name)
    target=hit
    typeset -n r=target
    eval 'printf "refers_to=%s value=%s\n" "${!r}" "$r"'
    unset r
    eval 'printf "after_unset target=%s refers_to=%s\n" "${target-UNSET}" "${!r}"'
    ;;
  unset-n)
    target=hit
    typeset -n r=target
    unset -n r
    printf 'unset_n_status=%s target=%s\n' "$?" "${target-UNSET}"
    ;;
  selfref)
    # shellcheck disable=SC2034 # the self-reference is the input under test
    typeset -n x=x
    printf 'selfref_status=%s\n' "$?"
    ;;
  circular-local)
    eval '
      function setter {
        typeset -n out=$1
        out=assigned
      }
      function call_setter {
        typeset out=caller_local
        setter out
        printf "caller_out=%s\n" "$out"
      }'
    out=global
    call_setter
    printf 'global_out=%s\n' "$out"
    ;;
  asym)
    eval '
      function holder {
        typeset -n ref=target
        reader_writer
        printf "holder_target=%s\n" "$target"
      }
      function reader_writer {
        typeset target=callee_local
        printf "read_through_ref=%s\n" "$ref"
        ref=written
        printf "callee_target=%s\n" "$target"
      }'
    target=global
    holder
    printf 'global_target=%s\n' "$target"
    printf 'global_ref=%s\n' "${ref-UNSET}"
    ;;
  for)
    a=1 b=2 c=3
    typeset -n v
    for v in a b c; do
      v=$((v * 10))
    done
    printf 'a=%s b=%s c=%s\n' "$a" "$b" "$c"
    ;;
  positional)
    set -- first
    typeset -n r=1
    printf 'typeset_status=%s r=%s\n' "$?" "${r-UNSET}"
    ;;
  print)
    # shellcheck disable=SC2034 # t is read through the nameref r
    t=1
    typeset -n r=t
    typeset -p r
    ;;
  *)
    printf 'usage: nameref_probe.sh support|chain N|bind|name|unset-n|selfref|circular-local|asym|for|positional|print\n' >&2
    exit 64
    ;;
esac
