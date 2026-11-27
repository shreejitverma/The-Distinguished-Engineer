#!/bin/sh
# golden.sh: golden-file tests for command output, as a capture / scrub / check / update pipeline.
# A "golden set" is a directory holding three files: stdout, stderr, status.
# Usage:
#   golden.sh capture DIR CMD [ARG...]  run CMD with a scrubbed environment; write DIR/stdout, DIR/stderr, DIR/status
#   golden.sh scrub DIR [TMPDIR]        normalize volatile fields in DIR/stdout and DIR/stderr in place
#   golden.sh check GOLDEN ACTUAL       compare the three files byte for byte (cmp); show a diff for each difference
#   golden.sh update GOLDEN ACTUAL      replace GOLDEN with ACTUAL; refused when CI is set
# Exit status of check: 0 same, 1 different, 2 usage error or missing golden set.

die() {
  printf 'golden: %s\n' "$*" >&2
  exit 2
}

cmd_capture() {
  [ "$#" -ge 2 ] || die 'usage: capture DIR CMD [ARG...]'
  dir=$1
  shift
  mkdir -p "$dir" || die "cannot create $dir"
  # env -i drops every inherited variable, so the output cannot depend on the caller's environment.
  env -i PATH="$PATH" LC_ALL=C TZ=UTC "$@" >"$dir/stdout" 2>"$dir/stderr" </dev/null
  code=$?
  printf '%d\n' "$code" >"$dir/status"
}

cmd_scrub() {
  [ "$#" -ge 1 ] || die 'usage: scrub DIR [TMPDIR]'
  dir=$1
  tmp=${2-}
  for name in stdout stderr; do
    f=$dir/$name
    [ -f "$f" ] || die "missing $f"
    if [ -n "$tmp" ]; then
      # Escape the BRE metacharacters of the literal path, so "y.z" does not match "yzz".
      esc=$(printf '%s\n' "$tmp" | sed 's/[][\.*^$|]/\\&/g')
      set -- -e "s|$esc|<TMPDIR>|g"
    else
      set --
    fi
    sed ${1+"$@"} \
      -e 's/[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}T[0-9]\{2\}:[0-9]\{2\}:[0-9]\{2\}Z/<TIMESTAMP>/g' \
      -e 's/pid=[0-9][0-9]*/pid=<PID>/g' \
      "$f" >"$f.new" || die "sed failed on $f"
    mv "$f.new" "$f" || die "cannot replace $f"
  done
}

cmd_check() {
  [ "$#" -eq 2 ] || die 'usage: check GOLDEN ACTUAL'
  golden=$1
  actual=$2
  [ -d "$golden" ] || die "no golden set at $golden (create it deliberately with: update)"
  [ -d "$actual" ] || die "no actual set at $actual"
  rc=0
  for name in stdout stderr status; do
    [ -f "$golden/$name" ] || die "golden file missing: $golden/$name"
    [ -f "$actual/$name" ] || die "actual file missing: $actual/$name"
    if ! cmp -s "$golden/$name" "$actual/$name"; then
      rc=1
      printf 'golden: %s differs\n' "$name" >&2
      # The two header lines carry file names and mtimes, which are noise here.
      diff -u "$golden/$name" "$actual/$name" | sed '1,2d' >&2
    fi
  done
  return "$rc"
}

cmd_update() {
  [ "$#" -eq 2 ] || die 'usage: update GOLDEN ACTUAL'
  [ -z "${CI-}" ] || die 'refusing to update golden files when CI is set'
  golden=$1
  actual=$2
  for name in stdout stderr status; do
    [ -f "$actual/$name" ] || die "actual file missing: $actual/$name"
  done
  mkdir -p "$golden" || die "cannot create $golden"
  for name in stdout stderr status; do
    cp "$actual/$name" "$golden/.$name.new" || die "cannot copy $name"
    mv -f "$golden/.$name.new" "$golden/$name" || die "cannot replace $name"
  done
}

case ${1-} in
  capture)
    shift
    cmd_capture "$@"
    ;;
  scrub)
    shift
    cmd_scrub "$@"
    ;;
  check)
    shift
    cmd_check "$@"
    ;;
  update)
    shift
    cmd_update "$@"
    ;;
  *) die 'usage: golden.sh capture|scrub|check|update ...' ;;
esac
