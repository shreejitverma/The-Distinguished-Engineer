#!/bin/sh
# csv-tool.sh - CSV parsing, edge-case demonstration, and sanitization
# Demonstrates RFC 4180 state-machine parsing vs naive Unix line tools.

set -eu

usage() {
  printf 'Usage: %s <naive-cut|naive-read|parse-fsm|sanitize|strip-bom> [file]\n' "$0" >&2
  exit 1
}

[ "$#" -ge 1 ] || usage
mode="$1"
shift

# Read from specified file or standard input
input="${1:-/dev/stdin}"

case "$mode" in
  naive-cut)
    # Naive extraction of first two fields using cut
    # Fails when fields contain embedded commas or newlines
    cut -d, -f1,2 "$input"
    ;;

  naive-read)
    # Naive line-oriented shell loop with IFS=,
    # Fails on embedded commas, multi-line records, and leaves \r on CRLF
    while IFS=, read -r col1 col2 remainder; do
      printf 'col1=[%s] col2=[%s] rem=[%s]\n' "$col1" "$col2" "$remainder"
    done <"$input"
    ;;

  parse-fsm)
    # Pure POSIX awk finite state machine implementing RFC 4180 parsing
    # Handles:
    # 1. Comma delimiters outside quotes
    # 2. Quoted fields containing literal commas
    # 3. Quoted fields containing literal newlines (CR, LF, CRLF)
    # 4. Escaped double quotes represented as two adjacent double quotes ("")
    # 5. CRLF line endings (strips CR before LF at record end)
    # Outputs each record on a single line with fields enclosed in <...>
    awk '
      BEGIN {
        state = 0 # 0: field start / unquoted, 1: inside quoted field
        field = ""
        nfields = 0
      }
      {
        line = $0
        len = length(line)
        for (i = 1; i <= len; i++) {
          c = substr(line, i, 1)
          if (state == 0) {
            if (c == "\"") {
              state = 1
            } else if (c == ",") {
              nfields++
              fields[nfields] = field
              field = ""
            } else if (c == "\r") {
              # Ignore trailing CR before record newline
            } else {
              field = field c
            }
          } else if (state == 1) {
            if (c == "\"") {
              # Lookahead for doubled quote
              if (i < len && substr(line, i + 1, 1) == "\"") {
                field = field "\""
                i++
              } else {
                state = 0
              }
            } else {
              field = field c
            }
          }
        }
        if (state == 1) {
          # Embedded newline inside quoted field
          field = field "\n"
        } else {
          # End of record
          nfields++
          fields[nfields] = field
          field = ""
          # Emit record
          printf "%d fields:", nfields
          for (k = 1; k <= nfields; k++) {
            printf " <%s>", fields[k]
          }
          printf "\n"
          nfields = 0
        }
      }
      END {
        if (state == 1) {
          printf "ERROR: unclosed quote at end of input\n" > "/dev/stderr"
          exit 2
        }
      }
    ' "$input"
    ;;

  sanitize)
    # Neutralize CSV formula injection (CWE-1236)
    # Prefixes formula-triggering characters (=, +, -, @, tab, CR) with single quote
    # Wraps fields in RFC 4180 double quotes, escaping existing double quotes as ""
    awk -F, '
      function sanitize_cell(cell) {
        # Check if first character triggers formula execution
        first = substr(cell, 1, 1)
        if (first == "=" || first == "+" || first == "-" || first == "@" || first == "\t" || first == "\r") {
          # \047 is a single quote; POSIX awk defines octal escapes, not \x
          cell = "\047" cell
        }
        # Escape any internal double quotes by doubling them
        gsub(/"/, "\"\"", cell)
        return "\"" cell "\""
      }
      {
        # Split line by comma for demo sanitization of unquoted tokens
        out = ""
        for (i = 1; i <= NF; i++) {
          sc = sanitize_cell($i)
          out = (i == 1) ? sc : (out "," sc)
        }
        print out
      }
    ' "$input"
    ;;

  strip-bom)
    # Detect and remove UTF-8 Byte Order Mark (0xEF 0xBB 0xBF)
    # LC_ALL=C makes substr count bytes: in a UTF-8 locale gawk and the
    # One True Awk count the BOM as one character and never match it
    LC_ALL=C awk '
      NR == 1 {
        # Match leading 3-byte UTF-8 BOM (octal escapes are POSIX)
        if (substr($0, 1, 3) == "\357\273\277") {
          $0 = substr($0, 4)
        }
      }
      { print }
    ' "$input"
    ;;

  *)
    usage
    ;;
esac
