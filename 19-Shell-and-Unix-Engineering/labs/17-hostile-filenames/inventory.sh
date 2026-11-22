#!/bin/sh
# inventory.sh: Safely scan a directory for hostile filenames and report findings.
# Demonstrates robust path normalization, option immunity via ./ prefix,
# redirection-based inspection, and classification of filename attack vectors.
set -eu

dir="${1:-.}"

# Ensure directory path begins with safe prefix so "$dir"/* never yields a leading hyphen.
case "$dir" in
  /* | ./* | ../*) ;;
  *) dir="./$dir" ;;
esac

if [ ! -d "$dir" ]; then
  printf 'error: directory %s does not exist\n' "$dir" >&2
  exit 1
fi

nl=$(printf '\nx')
nl=${nl%x}

total=0
hostile=0

# Loop safely over files using globbing with guaranteed safe prefix.
for entry in "$dir"/*; do
  # Handle empty directory where glob expands to literal pattern.
  if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
    continue
  fi

  total=$((total + 1))
  base="${entry##*/}"
  flags=""

  # Vector 1: Leading hyphen (argument injection risk).
  case "$base" in
    -*) flags="${flags:+$flags,}HYPHEN" ;;
  esac

  # Vector 2: Embedded newline (line-oriented stream injection).
  case "$base" in
    *"$nl"*) flags="${flags:+$flags,}NEWLINE" ;;
  esac

  # Vector 3: Whitespace (field-splitting risk).
  case "$base" in
    *[[:space:]]*) flags="${flags:+$flags,}SPACE" ;;
  esac

  # Vector 4: Glob metacharacters (unquoted pathname expansion risk).
  case "$base" in
    *['*?[']*) flags="${flags:+$flags,}GLOB" ;;
  esac

  # Vector 5: Control characters (terminal injection risk).
  case "$base" in
    *[[:cntrl:]]*) flags="${flags:+$flags,}CONTROL" ;;
  esac

  if [ -n "$flags" ]; then
    hostile=$((hostile + 1))
    # Safely obtain size via redirection; never pass entry as argument to utilities that lack --.
    size=$(wc -c <"$entry" | tr -d ' ')
    printf 'HOSTILE: %s | flags: [%s] | bytes: %s\n' "$base" "$flags" "$size"
  fi
done

printf 'SUMMARY: total=%d hostile=%d clean=%d\n' "$total" "$hostile" "$((total - hostile))"
