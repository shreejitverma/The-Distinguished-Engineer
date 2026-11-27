#!/bin/sh
# safe-yaml-update.sh: safely update a field in a YAML file in-place.
# Avoids symlink destruction, cross-device EXDEV rename errors,
# and YAML type coercion traps.
# Requires: mikefarah/yq v4.
set -eu

show_help() {
  printf 'Usage: %s [OPTIONS] FILE PATH VALUE\n' "$0"
  printf 'Options:\n'
  printf '  --as-string        Force the value to be stored as a string (tag: !!str)\n'
  printf '  --isolate-anchors  Explode aliases first to prevent cross-anchor corruption\n'
  printf '  --help             Show this help\n'
}

force_string=0
isolate_anchors=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --as-string)
      force_string=1
      shift
      ;;
    --isolate-anchors)
      isolate_anchors=1
      shift
      ;;
    --help | -h)
      show_help
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      printf 'Error: unrecognized option: %s\n' "$1" >&2
      show_help >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if [ "$#" -ne 3 ]; then
  printf 'Error: expected exactly 3 arguments (FILE, PATH, VALUE), got %d\n' "$#" >&2
  show_help >&2
  exit 2
fi

input_file=$1
yaml_path=$2
new_val=$3

if [ ! -e "$input_file" ]; then
  printf 'Error: file not found: %s\n' "$input_file" >&2
  exit 1
fi

# Detect yq variant; this script targets mikefarah/yq v4 expression syntax.
if ! command -v yq >/dev/null 2>&1; then
  printf 'Error: yq is not installed\n' >&2
  exit 1
fi

raw_ver=$(yq --version 2>&1 || true)
case "$raw_ver" in
  *"mikefarah"*)
    ;;
  *)
    printf 'Error: safe-yaml-update requires mikefarah/yq (Go engine), found: %s\n' "$raw_ver" >&2
    exit 1
    ;;
esac

# 1. Resolve symlinks: under POSIX rename(2), renaming a file onto a symlink
# replaces the symlink inode itself, severing the link and leaving the target
# file untouched. To preserve the symlink, we must mutate the real target.
target_file=$input_file
while [ -h "$target_file" ]; do
  dest=$(readlink "$target_file") || {
    printf 'Error: failed to readlink %s\n' "$target_file" >&2
    exit 1
  }
  case "$dest" in
    /*)
      target_file=$dest
      ;;
    *)
      link_dir=$(dirname "$target_file")
      target_file="$link_dir/$dest"
      ;;
  esac
done

# 2. Check for hazardous scalar types requiring string quoting:
# Norway problem (all 22 YAML 1.1 boolean tokens): y/n, yes/no, on/off, true/false
# Octal numbers: 0644, 0755, 0o644
# Sexagesimal numbers: 22:22
case "$new_val" in
  NO | no | No | YES | yes | Yes | ON | on | On | OFF | off | Off | true | false | TRUE | FALSE | True | False | y | Y | n | N)
    force_string=1
    ;;
  0[0-7]* | 0o[0-7]* | 0O[0-7]*)
    force_string=1
    ;;
  [0-9]*:[0-9]*)
    force_string=1
    ;;
  *)
    ;;
esac

# 3. Create temporary file in the SAME directory as the target file.
# Using /tmp can cross filesystem mount points, triggering EXDEV on rename(2).
target_dir=$(dirname "$target_file")
tmp_file=$(mktemp "$target_dir/.safe-yaml.XXXXXX")
trap 'rm -f "$tmp_file"' EXIT INT TERM
# mktemp creates the file with mode 0600; copy the target's mode bits (and
# owner, where permitted) so the rename does not silently tighten permissions.
cp -p "$target_file" "$tmp_file"

# 4. Construct the yq expression.
# If isolating anchors, run explode(.) first so updating an aliased node
# does not silently mutate the base anchor or sibling references.
expr=""
if [ "$isolate_anchors" -eq 1 ]; then
  expr="explode(.) | "
fi

# Inject value via env variable to prevent expression injection.
# Assign and export in two statements so posh 0.14.1 does not field-split.
SAFE_YQ_VAL=$new_val
export SAFE_YQ_VAL
if [ "$force_string" -eq 1 ]; then
  expr="${expr}${yaml_path} = env(SAFE_YQ_VAL) | ${yaml_path} tag = \"!!str\""
else
  expr="${expr}${yaml_path} = env(SAFE_YQ_VAL)"
fi

# 5. Process target into the temporary file.
yq eval "$expr" "$target_file" >"$tmp_file"

# 6. Atomic replacement via rename(2).
mv "$tmp_file" "$target_file"
trap - EXIT INT TERM
