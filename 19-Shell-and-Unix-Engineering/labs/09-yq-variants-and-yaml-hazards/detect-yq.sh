#!/bin/sh
# detect-yq.sh: probe the installed yq implementation and print variant and version.
# Portable across POSIX sh, bash, zsh, dash, and busybox ash.
set -eu

show_help() {
  printf 'Usage: %s [--variant|--help]\n' "$0"
  printf 'Probe the installed yq binary and report whether it is mikefarah (Go),\n'
  printf 'kislyuk (Python), an unknown implementation, or none.\n'
}

mode="all"
if [ "$#" -gt 0 ]; then
  case "$1" in
    --variant)
      mode="variant"
      ;;
    --help | -h)
      show_help
      exit 0
      ;;
    *)
      printf 'Error: unrecognized option %s\n' "$1" >&2
      show_help >&2
      exit 2
      ;;
  esac
fi

if ! command -v yq >/dev/null 2>&1; then
  if [ "$mode" = "variant" ]; then
    printf 'none\n'
  else
    printf 'variant=none\n'
    printf 'version=none\n'
  fi
  exit 1
fi

# Capture version output and help output safely.
raw_version=$(yq --version 2>&1 || true)
raw_help=$(yq --help 2>&1 || true)

variant="unknown"
case "$raw_version" in
  *"mikefarah"*)
    variant="mikefarah"
    ;;
  *)
    case "$raw_help" in
      *"--yaml-output"* | *"--yaml-roundtrip"* | *"kislyuk"*)
        variant="kislyuk"
        ;;
      *)
        # Check if -y flag is accepted (kislyuk) or rejected (mikefarah)
        if yq -y . </dev/null >/dev/null 2>&1; then
          variant="kislyuk"
        fi
        ;;
    esac
    ;;
esac

if [ "$mode" = "variant" ]; then
  printf '%s\n' "$variant"
else
  printf 'variant=%s\n' "$variant"
  printf 'raw_version=%s\n' "$raw_version"
fi
