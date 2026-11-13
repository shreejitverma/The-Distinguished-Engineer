#!/bin/sh
# extglob-probe.sh MODE [ARG]: the failure modes of extended patterns, one per MODE.
#   negation DIR          run `!(true)` in DIR with the shell's defaults, print its status
#   negation-extglob DIR  the same after `shopt -s extglob` (a no-op outside bash)
#   params                trim and pick suffixes with extended patterns in ${v##p} and ${v%p}
#   squeeze               collapse runs of blanks with ${v//+( )/ } and anchor with ${v/#p/r}
#   backtrack N           time *(*(*(a|b))) against L-1 a's plus "!" (no match) for L = 10, 11, ...
#                         until one match takes 2 clock seconds or L = N; print L and the seconds
#   deep N                match *([0-9]) against N digits under an 8 MiB stack limit
# The extended-pattern text always sits in an eval string, so every shell can parse this file.

enable_ext() {
  if command -v shopt >/dev/null 2>&1; then
    # shellcheck disable=SC3044 # bash-only builtin, reached only when command -v finds it
    shopt -s extglob && ext_mode=shopt
  elif command -v setopt >/dev/null 2>&1; then
    setopt kshglob && ext_mode=setopt
  else
    ext_mode=native
  fi
  if ! (eval 'case ab in @(ab|cd)) exit 0 ;; *) exit 1 ;; esac') 2>/dev/null; then
    ext_mode=none
  fi
}

# digits N: print a string of N zero digits without a trailing newline.
digits() {
  printf "%0${1}d" 0
}

mode=$1
case $mode in
  negation | negation-extglob)
    cd -- "$2" || exit 1
    if [ "$mode" = negation-extglob ]; then
      # shellcheck disable=SC3044 # the bash switch is the point; other shells ignore the failure
      shopt -s extglob 2>/dev/null
    fi
    # dash and bash by default read this as `! ( true )`, a negated subshell, status 1;
    # POSIX.1-2024 XCU 2.9.2 leaves `!` immediately followed by `(` unspecified.
    # With ksh-style patterns on, the parser reads one word, a pattern that matches every name
    # except "true", and runs the first match as a command.
    eval '!(true)'
    printf 'status=%s\n' "$?"
    ;;
  params)
    enable_ext
    printf 'mode=%s\n' "$ext_mode"
    eval 'v="  a  b  "; t=${v##+([[:space:]])}; t=${t%%+([[:space:]])}; printf "<%s>\n" "$t"'
    eval 'p=/a/b///; printf "%s\n" "${p%%+(/)}"'
    eval 'f=x.tar.gz; printf "%s %s %s\n" "${f%.@(gz|bz2)}" "${f%%.*([!.])}" "${f##*(x|.tar)}"'
    ;;
  squeeze)
    enable_ext
    printf 'mode=%s\n' "$ext_mode"
    eval 'v="a   b    c"; printf "<%s>\n" "${v//+( )/ }" "${v/#@(a|b)/X}"'
    ;;
  backtrack)
    enable_ext
    printf 'mode=%s\n' "$ext_mode"
    [ "$ext_mode" != none ] || exit 3
    # Positive control: the same pattern must match "abab", or the timing proves nothing.
    control=none r=none
    eval 'case abab in *(*(*(a|b)))) control=1 ;; *) control=0 ;; esac'
    # Grow the input one byte at a time until one failed match takes 2 clock seconds (at least
    # 1 s of real time) or the length reaches N; an exponential matcher stops early, a linear
    # one reaches N.
    n=10
    while :; do
      s=$(digits "$((n - 1))" | tr 0 a)!
      start=$(date +%s)
      eval 'case $s in *(*(*(a|b)))) r=match ;; *) r=nomatch ;; esac'
      secs=$(($(date +%s) - start))
      if [ "$secs" -ge 2 ] || [ "$n" -ge "$2" ]; then
        break
      fi
      n=$((n + 1))
    done
    printf 'control=%s len=%s result=%s secs=%s\n' "$control" "$n" "$r" "$secs"
    ;;
  deep)
    enable_ext
    printf 'mode=%s\n' "$ext_mode"
    [ "$ext_mode" != none ] || exit 3
    # shellcheck disable=SC3045 # every shell that reaches this line has ulimit -c
    ulimit -c 0 2>/dev/null
    # shellcheck disable=SC3045 # every shell that reaches this line has ulimit -s
    ulimit -s 8192 2>/dev/null
    s=$(digits "$2")
    r=none
    eval 'case $s in *([0-9])) r=match ;; *) r=nomatch ;; esac'
    printf 'len=%s result=%s\n' "${#s}" "$r"
    ;;
  *)
    printf 'usage: %s negation|negation-extglob DIR | params | squeeze | backtrack N | deep N\n' "$0" >&2
    exit 2
    ;;
esac
