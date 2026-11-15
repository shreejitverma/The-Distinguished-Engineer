#!/bin/sh
# addresses.sh [SED [ARG...]]: run portable sed address and branching programs (only what
# POSIX.1-2024 requires) and print one "name: result" line each, with output lines joined by "|".
# Scripts with labels are passed as separate -e options, which POSIX joins with newlines,
# so every label ends at a newline, as FreeBSD and macOS sed require.
set -u
if [ "$#" -eq 0 ]; then
  set -- sed
fi
dir=$(mktemp -d "${TMPDIR:-/tmp}/sedaddr.XXXXXX") || exit 1
trap 'rm -rf "$dir"' EXIT

# show NAME: read the program's output on stdin and print "NAME: line1|line2|...".
show() {
  awk -v name="$1" 'BEGIN { printf "%s: ", name } { printf "%s%s", sep, $0; sep = "|" } END { print "" }'
}

# 1. Two numeric addresses select an inclusive range.
printf '%s\n' 1 2 3 4 5 6 | "$@" -n '2,4p' | show 'range 2,4'

# 2. A second address <= the line that opened the range selects one line only.
printf '%s\n' 1 2 3 4 5 6 | "$@" -n '4,2p' | show 'range 4,2'

# 3. The end regex is first tried on the line after the start, so a range spans
#    at least two lines, and after it closes sed looks for the start again.
printf '%s\n' x a x b x c | "$@" -n '/x/,/x/p' | show 'range /x/,/x/'

# 4. "!" inverts the selection: keep only the BEGIN..END blocks.
printf '%s\n' pre BEGIN x y END post | "$@" '/^BEGIN$/,/^END$/!d' | show 'keep blocks'

# 5. 1,/re/ cannot end on line 1; this replaces only the first match in the
#    whole stream portably: n prints and reads the next line, "b a" loops until
#    n finds no input and quits.
printf '%s\n' k=1 x k=2 k=3 | "$@" -e '/^k=/{' -e 's//K=/' -e ':a' -e 'n' -e 'b a' -e '}' \
  | show 'first match only'

# 6. A loop: join lines that end in a backslash. "$!N" never runs N on the last line.
printf '%s\n' 'a \' 'b \' c d | "$@" -e ':a' -e '/\\$/{' -e '$!N' -e 's/\\\n//' -e 't a' -e '}' \
  | show 'join continuations'

# 7. A t loop: insert thousands separators, one per successful substitution.
printf '%s\n' 1234567 12 | "$@" -e ':a' -e 's/\(.*[0-9]\)\([0-9]\{3\}\)/\1,\2/' -e 't a' \
  | show 'thousands'

# 8. Slurp the whole input with a newline-terminated label, then edit it as one string.
#    "$!{" guards N, so a one-line input never reaches N at end of input (BSD drops it there).
printf '%s\n' a b c | "$@" -e ':a' -e '$!{' -e 'N' -e 'b a' -e '}' -e 's/\n/+/g' | show 'slurp'
printf '%s\n' a | "$@" -e ':a' -e '$!{' -e 'N' -e 'b a' -e '}' -e 's/\n/+/g' | show 'slurp 1 line'

# 9. Line numbers and $ span all file operands: line 3 is the first line of the second file.
printf '%s\n' a b >"$dir/f1"
printf '%s\n' c d >"$dir/f2"
"$@" -n -e '2p' -e '3p' -e '$p' "$dir/f1" "$dir/f2" | show 'two files'

# 10. c with a range writes its text once, at the end of the range; with a
#     negated single address it writes the text for every selected line.
#     The text follows "c\" and a newline inside one script string: BSD sed does
#     not continue the text into the next -e option (see sed-probe.sh c-split-e).
printf '%s\n' 1 2 3 4 | "$@" '2,3c\
X' | show 'c on 2,3'
printf '%s\n' 1 2 3 | "$@" '3!c\
X' | show 'c on 3!'
