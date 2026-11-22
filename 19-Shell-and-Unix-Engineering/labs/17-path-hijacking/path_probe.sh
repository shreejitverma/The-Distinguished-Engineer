#!/bin/sh
# path_probe.sh - PATH audit, resolution, and sanitization probe
# Usage:
#   path_probe.sh audit [PATH_STRING]
#   path_probe.sh resolve COMMAND [PATH_STRING]
#   path_probe.sh sanitize [PATH_STRING]

set -eu

mode="${1:-}"
shift || {
  printf 'usage: path_probe.sh <audit|resolve|sanitize> [args...]\n' >&2
  exit 2
}

do_audit() {
  target_path="${PATH:-}"
  if [ "$#" -gt 0 ]; then
    target_path="$1"
  fi

  if [ -z "$target_path" ]; then
    printf 'DANGER: PATH is empty (resolves to current working directory)\n'
    return 0
  fi

  rest="$target_path"
  while :; do
    entry=""
    had_colon=0
    case "$rest" in
      *:*)
        entry="${rest%%:*}"
        rest="${rest#*:}"
        had_colon=1
        ;;
      *)
        entry="$rest"
        rest=""
        ;;
    esac

    if [ -z "$entry" ]; then
      printf 'DANGER: empty element in PATH (resolves to cwd)\n'
    elif [ "$entry" = "." ]; then
      printf 'DANGER: explicit dot "." in PATH\n'
    elif case "$entry" in /*) false ;; *) true ;; esac then
      printf 'DANGER: relative directory "%s" in PATH\n' "$entry"
    elif [ ! -d "$entry" ]; then
      printf 'WARN: directory "%s" does not exist\n' "$entry"
    else
      # shellcheck disable=SC2012 # ls -ld used for portable directory mode bits
      perms=$(ls -ld "$entry" 2>/dev/null | cut -c 1-10)
      case "$perms" in
        ????????w[tT])
          printf 'OK: "%s" (sticky bit set)\n' "$entry"
          ;;
        ????????w?)
          printf 'DANGER: directory "%s" is world-writable without sticky bit\n' "$entry"
          ;;
        *)
          printf 'OK: "%s"\n' "$entry"
          ;;
      esac
    fi

    if [ "$had_colon" -eq 0 ] && [ -z "$rest" ]; then
      break
    fi
  done
}

do_resolve() {
  cmd="$1"
  target_path="${PATH:-}"
  if [ "$#" -gt 1 ]; then
    target_path="$2"
  fi

  if [ -z "$cmd" ]; then
    printf 'error: empty command name\n' >&2
    exit 2
  fi

  case "$cmd" in
    */*)
      if [ -f "$cmd" ] && [ -x "$cmd" ]; then
        printf 'DIRECT: %s\n' "$cmd"
        return 0
      fi
      printf 'NOT_FOUND: %s\n' "$cmd"
      return 1
      ;;
  esac

  if [ -z "$target_path" ]; then
    if [ -f "./$cmd" ] && [ -x "./$cmd" ]; then
      printf 'HIJACKED: ./%s (via empty PATH)\n' "$cmd"
      return 0
    fi
    printf 'NOT_FOUND: %s\n' "$cmd"
    return 1
  fi

  rest="$target_path"
  while :; do
    dir=""
    had_colon=0
    case "$rest" in
      *:*)
        dir="${rest%%:*}"
        rest="${rest#*:}"
        had_colon=1
        ;;
      *)
        dir="$rest"
        rest=""
        ;;
    esac

    candidate=""
    if [ -z "$dir" ] || [ "$dir" = "." ]; then
      candidate="./$cmd"
    else
      candidate="$dir/$cmd"
    fi

    if [ -f "$candidate" ] && [ -x "$candidate" ]; then
      case "$candidate" in
        ./*)
          printf 'HIJACKED: %s\n' "$candidate"
          return 0
          ;;
        /*)
          printf 'RESOLVED: %s\n' "$candidate"
          return 0
          ;;
        *)
          printf 'HIJACKED: %s\n' "$candidate"
          return 0
          ;;
      esac
    fi

    if [ "$had_colon" -eq 0 ] && [ -z "$rest" ]; then
      break
    fi
  done

  printf 'NOT_FOUND: %s\n' "$cmd"
  return 1
}

do_sanitize() {
  raw_path="${PATH:-}"
  if [ "$#" -gt 0 ]; then
    raw_path="$1"
  fi

  safe_path=""

  if [ -n "$raw_path" ]; then
    rest="$raw_path"
    while :; do
      entry=""
      had_colon=0
      case "$rest" in
        *:*)
          entry="${rest%%:*}"
          rest="${rest#*:}"
          had_colon=1
          ;;
        *)
          entry="$rest"
          rest=""
          ;;
      esac

      case "$entry" in
        "" | "." | ./* | ../*)
          ;;
        /*)
          if [ -d "$entry" ]; then
            # shellcheck disable=SC2012 # ls -ld used for portable directory mode bits
            perms=$(ls -ld "$entry" 2>/dev/null | cut -c 1-10)
            case "$perms" in
              ????????w[!tT] | ????????w)
                ;;
              *)
                if [ -z "$safe_path" ]; then
                  safe_path="$entry"
                else
                  safe_path="$safe_path:$entry"
                fi
                ;;
            esac
          fi
          ;;
        *)
          ;;
      esac

      if [ "$had_colon" -eq 0 ] && [ -z "$rest" ]; then
        break
      fi
    done
  fi

  if [ -z "$safe_path" ]; then
    safe_path=$(command -p getconf PATH 2>/dev/null || printf '/usr/bin:/bin')
  fi

  printf '%s\n' "$safe_path"
}

case "$mode" in
  audit)
    do_audit "$@"
    ;;
  resolve)
    [ "$#" -ge 1 ] || {
      printf 'usage: path_probe.sh resolve COMMAND [PATH]\n' >&2
      exit 2
    }
    do_resolve "$@"
    ;;
  sanitize)
    do_sanitize "$@"
    ;;
  *)
    printf 'unknown mode: %s\n' "$mode" >&2
    exit 2
    ;;
esac
