#!/bin/sh
# lint-shell.sh: lint every shell script under a directory with ShellCheck, choosing files by shebang.
# Usage: lint-shell.sh DIR
# Prints "ok PATH" or "fail PATH" (followed by ShellCheck's findings) for each file ShellCheck can
# analyze, "skip PATH" for every other file, then "checked N, failed M, skipped K".
# Exit status: 0 when every checked file is clean, 1 when a file has findings, 2 when the driver
# or ShellCheck itself could not run (ShellCheck statuses 2, 3 and 4 are invocation problems, not findings).
# Limits: file names containing a newline are not supported; `env` is followed only for one plain word.

# shebang_interp FILE: print the interpreter basename from the first line, or nothing for no shebang.
shebang_interp() {
  first=
  IFS= read -r first <"$1" || [ -n "$first" ] || return 0
  case $first in
    '#!'*) ;;
    *) return 0 ;;
  esac
  rest=${first#??}
  read -r word1 word2 _extra <<EOT
$rest
EOT
  interp=${word1##*/}
  if [ "$interp" = env ]; then
    interp=${word2##*/}
  fi
  printf '%s\n' "$interp"
}

dir=${1-}
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
  echo "usage: lint-shell.sh DIR" >&2
  exit 2
fi
if ! command -v shellcheck >/dev/null 2>&1; then
  echo "lint-shell.sh: shellcheck not found on PATH" >&2
  exit 2
fi

checked=0
failed=0
skipped=0
list=$(find "$dir" -type f | LC_ALL=C sort)
while IFS= read -r file; do
  [ -n "$file" ] || continue
  interp=$(shebang_interp "$file")
  case $interp in
    sh | ash | dash | bash | ksh | bats) ;;
    *)
      skipped=$((skipped + 1))
      printf 'skip %s\n' "$file"
      continue
      ;;
  esac
  checked=$((checked + 1))
  findings=$(shellcheck -f gcc "$file" 2>&1)
  status=$?
  case $status in
    0) printf 'ok %s\n' "$file" ;;
    1)
      failed=$((failed + 1))
      printf 'fail %s\n%s\n' "$file" "$findings"
      ;;
    *)
      printf 'lint-shell.sh: shellcheck exited %d on %s\n' "$status" "$file" >&2
      exit 2
      ;;
  esac
done <<EOT
$list
EOT
printf 'checked %d, failed %d, skipped %d\n' "$checked" "$failed" "$skipped"
[ "$failed" -eq 0 ] || exit 1
