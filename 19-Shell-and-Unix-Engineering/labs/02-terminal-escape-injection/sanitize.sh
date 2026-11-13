#!/bin/sh
# sanitize.sh: build and neutralize terminal-escape-injection payloads.
#
# Untrusted bytes written to a terminal are code: ESC, BEL, CR and other C0
# controls drive the emulator and can make it write bytes back onto your input
# queue.  The defence is to strip the control bytes at the boundary before the
# bytes ever reach the terminal, keeping only TAB (0x09) and LF (0x0a).
#
# Modes:
#   drop            Filter stdin to stdout, deleting every C0 control except TAB
#                   and LF (NUL included), and deleting DEL (0x7f): 31 byte
#                   values.  A neutralized OSC or CSI loses its ESC/BEL and
#                   renders as inert printable text.  Bytes 0x80 to 0xff pass
#                   through, so UTF-8 text survives; the C1 controls need a
#                   decoding step that this byte filter does not attempt.
#   payload NAME    Write a known hostile payload to stdout, for a test or the
#                   injection probe to consume.  NAME is one of:
#                     title    OSC 2 sets the window title to "id", then CSI 21 t
#                              asks the terminal to report the title back.
#                     osc52    OSC 52 asks the terminal to report the clipboard.
#                     decrqss  A DECRQSS request whose unknown argument some
#                              terminals echo back verbatim (CVE-2008-2383 class).
#
# ASCII source only; non-printable bytes are built at run time with printf octal.
set -eu

mode=${1:-}
case $mode in
  drop)
    # The delete set is tr's own octal notation: NUL to BS, VT to US, and DEL.
    # It cannot be a string of literal bytes, because a command argument cannot
    # carry NUL, and a set without NUL lets that C0 control through.
    # LC_ALL=C so tr works on bytes, not multibyte characters.
    LC_ALL=C tr -d '\000-\010\013-\037\177'
    ;;
  payload)
    name=${2:-}
    case $name in
      title)
        # ESC ] 2 ; id BEL   (set title)   ESC [ 21 t   (report title)
        printf '\033]2;id\007\033[21t'
        ;;
      osc52)
        # ESC ] 52 ; c ; ? BEL   (report clipboard, "c" selection)
        printf '\033]52;c;?\007'
        ;;
      decrqss)
        # ESC P $ q <query> ESC backslash   (DECRQSS, DCS framed).
        # Octal: \044 is "$" and \134 is backslash, kept out of the format
        # string so no shell or ShellCheck reads them as an expansion or escape.
        printf '\033P\044qm\033\134'
        ;;
      *)
        printf 'sanitize.sh: unknown payload %s\n' "$name" >&2
        exit 2
        ;;
    esac
    ;;
  *)
    printf 'usage: sanitize.sh drop | payload title|osc52|decrqss\n' >&2
    exit 2
    ;;
esac
