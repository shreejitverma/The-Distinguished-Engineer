#!/bin/sh
# csvcol: print one named column of CSV input, honoring RFC 4180 quoting
# (commas and newlines inside quoted fields), which awk -F, and cut -d, cannot.
# Usage: csvcol.sh COLUMN [FILE...]   (no FILE, or "-", reads standard input)
# Environment: PYTHON names the interpreter (default python3).
#
# The shell owns argument checking and process plumbing; Python owns parsing.
# The program travels on fd 3 from a quoted here-document, so the shell expands
# nothing in it and standard input stays free for data; values travel in argv.
set -eu
if [ "$#" -lt 1 ]; then
  echo "usage: csvcol.sh COLUMN [FILE...]" >&2
  exit 2
fi
exec "${PYTHON:-python3}" -I /dev/fd/3 "$@" 3<<'EOF'
import csv, os, signal, sys

def main(column, paths):
    for path in paths or ["-"]:
        if path == "-":
            stream = open(sys.stdin.fileno(), newline="", encoding="utf-8", closefd=False)
        else:
            stream = open(path, newline="", encoding="utf-8")
        with stream:
            reader = csv.DictReader(stream, restval="")
            if column not in (reader.fieldnames or []):
                sys.exit(f"csvcol: {path}: no column {column!r}")
            for row in reader:
                sys.stdout.write(row[column] + "\n")
    sys.stdout.flush()

try:
    main(sys.argv[1], sys.argv[2:])
except BrokenPipeError:
    # Die of SIGPIPE like any other filter, so the caller sees 128 + 13.
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGPIPE)
except (OSError, csv.Error, UnicodeDecodeError) as e:
    sys.exit(f"csvcol: {e}")
EOF
