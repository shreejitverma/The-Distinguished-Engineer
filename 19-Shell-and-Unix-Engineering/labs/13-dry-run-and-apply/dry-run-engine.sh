#!/bin/sh
# dry-run-engine.sh: core probe and execution engine for dry-run and apply patterns.
# Demonstrates syntax limits (set -n), redirection leakage, argument serialization,
# state simulation divergence, two-phase plan-and-apply, and safety latches.

set -eu

# quote_arg STRING: emit a safely quoted representation of STRING for POSIX sh.
quote_arg() {
  case "$1" in
    '')
      printf "''"
      ;;
    *[!A-Za-z0-9_.,:/-]*)
      # Replace every single quote with single quote, backslash, single quote, single quote.
      # The trailing x keeps command substitution from stripping trailing newlines.
      escaped=$(
        printf '%s\n' "$1" | sed "s/'/'\\\\''/g"
        printf 'x'
      )
      escaped=${escaped%?x}
      printf "'%s'" "$escaped"
      ;;
    *)
      printf '%s' "$1"
      ;;
  esac
}

# quote_cmd ARG...: emit all arguments quoted and space-separated.
quote_cmd() {
  first=1
  for arg in "$@"; do
    if [ "$first" -eq 1 ]; then
      first=0
    else
      printf ' '
    fi
    quote_arg "$arg"
  done
  printf '\n'
}

# naive_exec DRY_RUN CMD...: naive wrapper that prints when DRY_RUN=1.
naive_exec() {
  dry_run="$1"
  shift
  if [ "$dry_run" -eq 1 ]; then
    printf '[DRY-RUN] ' >&2
    quote_cmd "$@" >&2
    return 0
  fi
  "$@"
}

# safe_write DRY_RUN CONTENT TARGET_FILE: encapsulates file writing.
safe_write() {
  dry_run="$1"
  content="$2"
  target="$3"
  if [ "$dry_run" -eq 1 ]; then
    len=$(printf '%s' "$content" | wc -c | tr -d ' \t')
    printf '[DRY-RUN] write %d bytes to %s\n' "$len" "$target" >&2
    return 0
  fi
  printf '%s\n' "$content" >"$target"
}

# plan_sync SOURCE_DIR TARGET_DIR: computes declarative diff between two directories.
# Emits tab-separated action records: ACTION <relpath>
plan_sync() {
  src="$1"
  dst="$2"

  # Find all files in source
  (
    cd "$src" 2>/dev/null || exit 1
    find . -type f 2>/dev/null | LC_ALL=C sort
  ) | while IFS= read -r rel; do
    rel_clean=${rel#./}
    if [ ! -f "$dst/$rel_clean" ]; then
      printf 'ADD\t%s\n' "$rel_clean"
    else
      # Compare content
      if ! cmp -s "$src/$rel_clean" "$dst/$rel_clean"; then
        printf 'MODIFY\t%s\n' "$rel_clean"
      fi
    fi
  done

  # Find deleted files (present in dst but missing in src)
  if [ -d "$dst" ]; then
    (
      cd "$dst" 2>/dev/null || exit 1
      find . -type f 2>/dev/null | LC_ALL=C sort
    ) | while IFS= read -r rel; do
      rel_clean=${rel#./}
      if [ ! -f "$src/$rel_clean" ]; then
        printf 'DELETE\t%s\n' "$rel_clean"
      fi
    done
  fi
}

# apply_action ACTION RELPATH SOURCE_DIR TARGET_DIR
apply_action() {
  action="$1"
  rel="$2"
  src="$3"
  dst="$4"

  case "$action" in
    ADD | MODIFY)
      target_dir=$(dirname "$dst/$rel")
      [ -d "$target_dir" ] || mkdir -p "$target_dir"
      cp -f "$src/$rel" "$dst/$rel"
      ;;
    DELETE)
      rm -f "$dst/$rel"
      ;;
    *)
      printf 'Unknown action: %s\n' "$action" >&2
      return 1
      ;;
  esac
}

mode="${1:-help}"
shift || true

case "$mode" in
  redirection_leak)
    # Target file passed as argument
    file="$1"
    # Write initial known content
    printf 'initial content\n' >"$file"

    # Naive execution with redirection:
    # DRY_RUN=1 naive_exec echo "leak data" > "$file"
    # Redirection happens before naive_exec runs, truncating or writing to file!
    naive_exec 1 echo "leak data" >"$file" 2>/dev/null || true
    size_after_naive=$(wc -c <"$file" | tr -d ' \t')

    # Reset file
    printf 'initial content\n' >"$file"
    # Safe encapsulated write:
    safe_write 1 "leak data" "$file" 2>/dev/null || true
    content_after_safe=$(cat "$file")

    printf 'size_after_naive=%s\n' "$size_after_naive"
    printf 'content_after_safe=%s\n' "$content_after_safe"
    ;;

  quote_serialize)
    # Prints safely serialized command
    quote_cmd "$@"
    ;;

  simulation_divergence)
    # Demonstrates the simulation fallacy where step 2 relies on step 1's side effect
    workdir="$1"
    target="$workdir/new_dir"
    nested_file="$target/app.conf"

    # Naive dry run: step 1 (mkdir) is skipped
    # step 2: cd or write into target fails because target does not exist!
    naive_failed=0
    if [ ! -d "$target" ]; then
      # Simulating what happens when a subsequent command tries to touch a file in missing dir
      # without proper directory creation
      if ! touch "$nested_file" 2>/dev/null; then
        naive_failed=1
      fi
    fi

    # Two-phase plan approach:
    # Generates plan without relying on intermediate state existence
    plan_out="$workdir/plan.tsv"
    printf 'MKDIR\t%s\n' "$target" >"$plan_out"
    printf 'TOUCH\t%s\n' "$nested_file" >>"$plan_out"
    plan_count=$(wc -l <"$plan_out" | tr -d ' \t')

    printf 'naive_divergence_failed=%d\n' "$naive_failed"
    printf 'two_phase_plan_records=%d\n' "$plan_count"
    ;;

  sync)
    # Usage: sync [--dry-run|-n] [--apply] [--plan-out=FILE] [--plan-in=FILE] SRC DST
    dry_run=1
    apply=0
    plan_out=""
    plan_in=""

    while [ "$#" -gt 0 ]; do
      case "$1" in
        -n | --dry-run)
          dry_run=1
          shift
          ;;
        --apply)
          apply=1
          dry_run=0
          shift
          ;;
        --plan-out=*)
          plan_out="${1#*=}"
          shift
          ;;
        --plan-in=*)
          plan_in="${1#*=}"
          shift
          ;;
        --)
          shift
          break
          ;;
        -*)
          printf 'Unknown option: %s\n' "$1" >&2
          exit 1
          ;;
        *)
          break
          ;;
      esac
    done

    if [ "$#" -lt 2 ]; then
      printf 'Usage: dry-run-engine.sh sync [OPTIONS] SRC DST\n' >&2
      exit 1
    fi

    src_dir="$1"
    dst_dir="$2"

    if [ ! -d "$src_dir" ]; then
      printf 'Error: source directory %s does not exist\n' "$src_dir" >&2
      exit 1
    fi

    # Determine plan
    plan_data=""
    if [ -n "$plan_in" ]; then
      if [ ! -f "$plan_in" ]; then
        printf 'Error: plan file %s not found\n' "$plan_in" >&2
        exit 1
      fi
      plan_data=$(cat "$plan_in")
    else
      plan_data=$(plan_sync "$src_dir" "$dst_dir")
    fi

    if [ -n "$plan_out" ]; then
      printf '%s\n' "$plan_data" >"$plan_out"
    fi

    # Count pending actions
    if [ -z "$plan_data" ]; then
      action_count=0
    else
      action_count=$(printf '%s\n' "$plan_data" | grep -c '^[A-Z]' || true)
    fi

    if [ "$dry_run" -eq 1 ]; then
      printf 'Plan: %d actions pending\n' "$action_count"
      if [ "$action_count" -gt 0 ]; then
        printf '%s\n' "$plan_data"
        # In plan/dry-run mode, exit status 2 indicates changes pending (diff convention)
        exit 2
      fi
      # Exit 0 indicates no changes pending
      exit 0
    fi

    # Apply mode
    if [ "$apply" -eq 1 ]; then
      if [ "$action_count" -eq 0 ]; then
        printf 'Apply: 0 actions pending, nothing to do\n'
        exit 0
      fi

      printf '%s\n' "$plan_data" | while IFS="$(printf '\t')" read -r act rel; do
        [ -n "$act" ] || continue
        apply_action "$act" "$rel" "$src_dir" "$dst_dir"
      done

      printf 'Apply: %d actions applied successfully\n' "$action_count"
      exit 0
    fi

    # Neither dry-run nor apply specified or safety failure
    printf 'Error: Refusing to mutate without --apply\n' >&2
    exit 2
    ;;

  safety_latch)
    # Demonstrates non-interactive safety latch
    has_apply=0
    for arg in "$@"; do
      if [ "$arg" = "--apply" ]; then
        has_apply=1
      fi
    done

    if [ "$has_apply" -eq 0 ]; then
      printf 'Error: safety latch engaged. Non-interactive run requires --apply.\n' >&2
      exit 2
    fi

    printf 'Safety latch released: mutations authorized.\n'
    exit 0
    ;;

  *)
    printf 'Unknown mode: %s\n' "$mode" >&2
    exit 1
    ;;
esac
