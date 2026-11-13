#!/bin/sh
# term-client.sh MODE [ARG...]: a program that finds out what terminal it is running on.
# Every value printed is read from the environment, the kernel, or the terminal's own reply.
#   env           the shell's TERM, and the TERM an external child process inherits
#   size          the kernel's window size (stty size) and the shell's COLUMNS and LINES,
#                 before and after one external foreground command
#   identify      ask the terminal who it is with Primary Device Attributes (ESC [ c)
#   resize R C    ask the terminal to resize to R rows by C columns (ESC [ 8 ; R ; C t), wait for
#                 it with a Device Attributes round trip, then print the kernel's size, the shell's
#                 COLUMNS and LINES, and what a plain "stty size" child reports with the inherited
#                 environment
# identify and resize need a terminal on stdin and stdout (miniterm provides one).

mode=${1-}
esc=$(printf '\033')

# child_term: print the TERM an external process inherits, or "unset".
child_term() {
  awk 'BEGIN { if ("TERM" in ENVIRON) print ENVIRON["TERM"]; else print "unset" }'
}

# kernel_size: print "ROWS COLS" as the kernel holds them for this terminal. COLUMNS and LINES are
# removed from the child's environment first, because some stty implementations (BusyBox) let those
# two variables override the kernel's answer.
kernel_size() {
  (
    unset COLUMNS LINES
    stty size
  )
}

# ask_da: send Primary Device Attributes and read the reply into $reply, one byte at a time,
# until the final "c" or 2 seconds of silence (VMIN 0, VTIME 20); an empty $reply means no answer.
ask_da() {
  saved=$(stty -g)
  stty -echo -icanon min 0 time 20
  printf '\033[c'
  reply=
  while :; do
    c=$(
      dd bs=1 count=1 2>/dev/null
      echo x
    )
    c=${c%x}
    [ -n "$c" ] || break
    reply=$reply$c
    if [ "$c" = c ]; then
      break
    fi
  done
  stty "$saved"
}

# show_reply: print $reply with its leading ESC spelled out, or "none".
show_reply() {
  case $reply in
    "") printf 'reply=none\n' ;;
    "$esc"*) printf 'reply=ESC%s\n' "${reply#"$esc"}" ;;
    *) printf 'reply=?%s\n' "$reply" ;;
  esac
}

case $mode in
  env)
    child=$(child_term)
    printf 'shell=%s child=%s\n' "${TERM-unset}" "$child"
    ;;
  size)
    printf 'start: COLUMNS=%s LINES=%s\n' "${COLUMNS-unset}" "${LINES-unset}"
    printf 'kernel: '
    # stty runs as a foreground command of this shell, not inside $( ), because bash looks at
    # the window size only after a foreground command that the main shell itself waited for.
    stty size
    printf 'after: COLUMNS=%s LINES=%s\n' "${COLUMNS-unset}" "${LINES-unset}"
    ;;
  identify)
    ask_da
    printf 'TERM=%s\n' "${TERM-unset}"
    show_reply
    ;;
  resize)
    before=$(kernel_size)
    printf 'before: kernel=%s COLUMNS=%s LINES=%s\n' "$before" "${COLUMNS-unset}" "${LINES-unset}"
    printf '\033[8;%d;%dt' "$2" "$3"
    ask_da
    after=$(kernel_size)
    seen=$(stty size)
    printf 'after: kernel=%s COLUMNS=%s LINES=%s stty=%s\n' "$after" "${COLUMNS-unset}" "${LINES-unset}" "$seen"
    ;;
  *)
    printf 'usage: term-client.sh env|size|identify|resize R C\n' >&2
    exit 2
    ;;
esac
