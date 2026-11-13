#!/usr/bin/env bash
# coproc-bash.sh MODE [ARG]: probes of the bash coproc keyword (bash 4.0 and later).
#   fds            descriptor numbers of two coprocesses, and whether C_PID names a live process
#   eof            wc -l as a coprocess: write three lines, close the write end, read the count
#   lost           read after the coprocess has exited, through C[0] and through a saved copy
#   inherit FDCHK  which children can use the descriptors; FDCHK is the compiled fdcheck helper
#   sigpipe        write to a coprocess that has exited, with SIGPIPE at its default disposition
#   sigpipe-ignored  the same write with SIGPIPE ignored
#   status         the status of the coproc command itself and the status wait reports
# Under bash 3.2 the first coproc line is a syntax error, which is what the lab asserts there.

mode=${1:?usage: coproc-bash.sh fds|eof|lost|inherit FDCHK|sigpipe|sigpipe-ignored|status}

case $mode in
  fds)
    coproc C { cat; }
    coproc D { cat; }
    if [ -n "${C_PID-}" ] && kill -0 "$C_PID" 2>/dev/null; then live=yes; else live=no; fi
    echo "C=${C[0]} ${C[1]} D=${D[0]} ${D[1]} C_PID_live=$live"
    ;;
  eof)
    coproc W { wc -l; }
    exec {r}<&"${W[0]}" # keep a read descriptor of our own; see the lost mode for why
    printf 'a\nb\nc\n' >&"${W[1]}"
    w=${W[1]}
    exec {w}>&- # closing the write end is the only way wc sees end of file
    after=${W[1]-unset}
    read -r n <&"$r"
    echo "count=$n write_fd_after_close=$after"
    ;;
  lost)
    coproc C { echo hi; }
    exec {saved}<&"${C[0]}"
    /bin/sleep 1 # the coprocess exits at once; bash reaps it while it waits for sleep
    if [ -n "${C_PID+set}" ]; then vars="set"; else vars="unset"; fi
    if read -r x 2>/dev/null <&"${C[0]-}"; then direct=$x; else direct=failed; fi
    if read -r y <&"$saved"; then kept=$y; else kept=failed; fi
    echo "vars=$vars direct=$direct saved=$kept"
    ;;
  inherit)
    fdchk=${2:?inherit needs the path of the fdcheck helper}
    coproc C { cat; }
    ext=$("$fdchk" "${C[0]}" "${C[1]}")
    if (: >&"${C[1]}") 2>/dev/null; then sub=open; else sub=closed; fi
    if cs=$(: >&"${C[1]}" && echo open) 2>/dev/null; then :; else cs=closed; fi
    "$fdchk" 1 >&"${C[1]}" # an explicit redirection hands the descriptor over
    read -r via <&"${C[0]}"
    echo "external: $ext"
    echo "subshell=$sub comsub=$cs redirected: $via"
    ;;
  sigpipe | sigpipe-ignored)
    [ "$mode" = sigpipe-ignored ] && trap '' PIPE
    coproc C { read -r _; }
    exec {w}>&"${C[1]}"
    echo one >&"$w" # the coprocess reads this line and exits
    /bin/sleep 1
    echo two >&"$w" # no reader is left: EPIPE, and SIGPIPE unless it is ignored
    # shellcheck disable=SC2320 # the status of that echo is what this mode reports
    st=$?
    echo "write_status=$st"
    ;;
  status)
    coproc C {
      read -r _
      exit 7
    }
    started=$?
    pid=$C_PID
    echo go >&"${C[1]}"
    wait "$pid"
    echo "coproc_status=$started wait_status=$?"
    ;;
  *)
    echo "coproc-bash.sh: unknown mode: $mode" >&2
    exit 2
    ;;
esac
