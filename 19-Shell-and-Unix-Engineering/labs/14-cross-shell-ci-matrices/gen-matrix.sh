#!/bin/sh
# gen-matrix.sh: turn a list of shells into the JSON a GitHub Actions job feeds to fromJSON.
#
# Usage: gen-matrix.sh LISTFILE
#   LISTFILE  one cell per line: NAME IMAGE PACKAGES (three words); blank lines and # comments are skipped
#             NAME      [A-Za-z0-9._-]+, unique; it becomes the job name and the required-check name
#             IMAGE     [A-Za-z0-9._:/@-]+, the container image the cell runs in
#             PACKAGES  [A-Za-z0-9._+,-]+, the packages to install (comma separated)
# Prints one line, {"include":[{"shell":NAME,"image":IMAGE,"packages":PACKAGES},...]}, on standard output,
# and the cell count on standard error.
# Exit status: 0 on success; 1 when a line is malformed, a NAME repeats, there are no cells, or there
# are more than 256 (the limit GitHub applies to one matrix: a matrix generates at most 256 jobs per
# workflow run, https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
# No character that needs JSON escaping can pass validation, so the output is built with printf alone.

max_cells=256

fail() {
  printf 'gen-matrix: %s\n' "$*" >&2
  exit 1
}

[ "$#" -eq 1 ] || fail "usage: gen-matrix.sh LISTFILE"
[ -r "$1" ] || fail "cannot read '$1'"

count=0
names=' '
json='{"include":['
while read -r name image packages extra; do
  case $name in
    '' | '#'*) continue ;;
    *) ;;
  esac
  [ -n "$packages" ] || fail "line for '$name' needs NAME IMAGE PACKAGES"
  [ -z "$extra" ] || fail "line for '$name' has more than three words"
  case $name in
    *[!A-Za-z0-9._-]*) fail "bad NAME '$name'" ;;
    *) ;;
  esac
  case $image in
    *[!A-Za-z0-9._:/@-]*) fail "bad IMAGE '$image' for '$name'" ;;
    *) ;;
  esac
  case $packages in
    *[!A-Za-z0-9._+,-]*) fail "bad PACKAGES '$packages' for '$name'" ;;
    *) ;;
  esac
  case $names in
    *" $name "*) fail "NAME '$name' appears twice" ;;
    *) names="$names$name " ;;
  esac
  if [ "$count" -gt 0 ]; then
    json="$json,"
  fi
  json="$json{\"shell\":\"$name\",\"image\":\"$image\",\"packages\":\"$packages\"}"
  count=$((count + 1))
done <"$1"

[ "$count" -gt 0 ] || fail "no cells in '$1'"
[ "$count" -le "$max_cells" ] || fail "$count cells exceed the limit of $max_cells per matrix"
printf '%s]}\n' "$json"
printf '%d cells\n' "$count" >&2
