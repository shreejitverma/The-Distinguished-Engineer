#!/bin/sh
# Run a skip-4-keep-8 chain of head -c twice over one input, as the injected stage 1 does at scale.
# usage: head-chain-probe.sh file|pipe INPUTFILE
# file: the input is a seekable standard input (POSIX XCU 1.4 INPUT FILES requires the offset to carry over).
# pipe: the input arrives through a pipe, so each head must read no more than it needs.
if [ "$#" -ne 2 ]; then
  echo "usage: head-chain-probe.sh file|pipe INPUTFILE" >&2
  exit 2
fi
chain='((head -c 4 >/dev/null) && head -c 8 && (head -c 4 >/dev/null) && head -c 8)'
case $1 in
  file) eval "$chain" <"$2" ;;
  pipe) cat "$2" | eval "$chain" ;;
  *)
    echo "mode must be file or pipe" >&2
    exit 2
    ;;
esac
