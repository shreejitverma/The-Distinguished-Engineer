#!/bin/sh
# report: line coverage of one source file from kcov-style traces.
# Usage: report.sh SOURCE TRACEFILE...
# Prints one row per source line: MARK LINENO TEXT, where MARK is
#   +  executable and hit    -  executable and not hit    .  not code (blank, comment, syntax, here-doc body)
# then a summary row: covered/total lines covered (PERCENT%).
# The code/non-code split is a simplified version of the line classifier in kcov's bash engine.
src=$1
shift
awk -v src="$src" '
function trim(s) {
  sub(/^[ \t]+/, "", s)
  sub(/[ \t]+$/, "", s)
  return s
}
{
  # A trace line may begin with extra "k" characters (nested expansion levels), so search for the marker.
  pos = index($0, "kcov@" src "@")
  if (pos == 0) next
  rest = substr($0, pos + length("kcov@" src "@"))
  if (match(rest, /^[0-9]+@/)) hit[substr(rest, 1, RLENGTH - 1) + 0] = 1
}
END {
  q = sprintf("%c", 39)
  heredoc_re = "<<-?[ ]*[A-Za-z_\"" q "]"
  quote_re = "[\"" q "]"
  heredoc = ""
  incase = 0
  n = 0
  covered = 0
  total = 0
  while ((getline line < src) > 0) {
    n++
    s = trim(line)
    code = 1
    if (heredoc != "") {
      code = 0
      if (s == heredoc) heredoc = ""
    } else if (s == "" || substr(s, 1, 1) == "#") {
      code = 0
    } else if (s ~ /^([{}]|esac|fi|done|then|else|do)$/) {
      code = 0
    } else if (s ~ /^[A-Za-z_][A-Za-z0-9_]*[ ]*[(][)][ ]*[{]?$/) {
      code = 0
    } else if (incase && s ~ /[)]$/) {
      code = 0
    } else if (s ~ heredoc_re) {
      w = s
      sub(/^.*<<-?[ ]*/, "", w)
      sub(/[ \t;|&].*$/, "", w)
      gsub(quote_re, "", w)
      heredoc = w
    }
    if (s ~ /^case[ ]/) incase = 1
    if (s ~ /^esac$/) incase = 0
    mark = "."
    if (code) {
      total++
      if (n in hit) {
        mark = "+"
        covered++
      } else {
        mark = "-"
      }
    }
    printf "%s %3d %s\n", mark, n, line
  }
  pct = (total > 0) ? int(100 * covered / total) : 0
  printf "%d/%d lines covered (%d%%)\n", covered, total, pct
}' "$@"
