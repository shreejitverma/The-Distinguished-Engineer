#!/usr/bin/env bash
# index-guard.sh MODE INDEX: print one element of the array (red green blue).
#   raw      prints "${colors[$INDEX]}" directly. The shell evaluates INDEX as an arithmetic
#            expression, so a value such as x[$(cmd)] runs cmd (bash, mksh, ksh93u+ 2012).
#   guarded  accepts INDEX only when it is a canonical decimal (0, or digits without a leading
#            zero, at most 9 of them) below the element count; anything else exits 2 before
#            the shell ever evaluates it.
# Exit status: 0 element printed, 2 index rejected, 64 usage error.
colors=(red green blue)

[ "$#" -eq 2 ] || exit 64
mode=$1
idx=$2

case $mode in
  raw)
    printf '%s\n' "${colors[$idx]}"
    ;;
  guarded)
    case $idx in
      '' | *[!0-9]* | 0?*) exit 2 ;;
      *) ;;
    esac
    [ "${#idx}" -le 9 ] || exit 2
    # The count of elements, spelled so that yash (where ${#colors[@]} lists lengths) agrees.
    set -- "${colors[@]}"
    [ "$idx" -lt "$#" ] || exit 2
    printf '%s\n' "${colors[idx]}"
    ;;
  *)
    exit 64
    ;;
esac
