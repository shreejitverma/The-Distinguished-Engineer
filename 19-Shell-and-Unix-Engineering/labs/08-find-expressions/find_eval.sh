#!/bin/sh
set -eu

mode="${1:-}"
dir="${2:-.}"

case "$mode" in
  precedence_unparenthesized)
    # shellcheck disable=SC2146 # intentionally demonstrating the unparenthesized -o precedence pitfall
    find "$dir" -name "*.txt" -o -name "*.md" -print | sort
    ;;
  precedence_parenthesized)
    find "$dir" \( -name "*.txt" -o -name "*.md" \) -print | sort
    ;;
  default_print)
    find "$dir" -name "*.txt" -o -name "*.md" | sort
    ;;
  prune_bare)
    find "$dir" -name "skip" -prune | sort
    ;;
  prune_correct)
    find "$dir" -name "skip" -prune -o -name "*.txt" -print | sort
    ;;
  prune_depth)
    find "$dir" -depth -name "skip" -prune -o -name "*.txt" -print | sort
    ;;
  short_circuit)
    trace_file="${3:?missing trace file}"
    find "$dir" -name "*.txt" -exec sh -c 'printf "%s\n" "$1" >> "$2"' _ {} "$trace_file" \;
    ;;
  safe_path)
    target="$dir"
    case "$target" in
      -*) target="./$target" ;;
      *) ;;
    esac
    find "$target" -name "*.txt" | sort
    ;;
  double_dash)
    find -- "$dir" -name "*.txt" | LC_ALL=C sort
    ;;
  double_dash_hyphen)
    # `--` ends the leading options only, so a hyphen-led operand is still read as an expression.
    if find -- "$dir" >/dev/null 2>&1; then
      printf 'accepted\n'
    else
      printf 'rejected\n'
    fi
    ;;
  delete_prune)
    # -delete implies -depth, which makes -prune a no-op; GNU find refuses this combination outright.
    find "$dir" -name "skip" -prune -o -name "*.txt" -delete
    find "$dir" | LC_ALL=C sort
    ;;
  delete_prune_depth)
    find "$dir" -depth -name "skip" -prune -o -name "*.txt" -delete
    find "$dir" | LC_ALL=C sort
    ;;
  size_units)
    for spec in 1k 2k 1025c 3 -2k +1k; do
      hits=$(find "$dir" -name "sized.bin" -size "$spec")
      if [ -n "$hits" ]; then
        printf '%s yes\n' "$spec"
      else
        printf '%s no\n' "$spec"
      fi
    done
    ;;
  *)
    printf 'Unknown mode: %s\n' "$mode" >&2
    exit 1
    ;;
esac
