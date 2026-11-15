#!/usr/bin/env bats
# Tests for saferm.sh: every dangerous operand is refused before rm runs, and a
# legitimate one is removed by its physical path.

setup() {
  load ../lib/lab
  SAFERM="$BATS_TEST_DIRNAME/saferm.sh"
  T=$BATS_TEST_TMPDIR
  ROOT=$T/root
  mkdir -p "$ROOT/cache/sub" "$T/outside" "$T/stub"
  : >"$ROOT/cache/sub/f"
  : >"$T/outside/keep"
  # PROOT is ROOT with symlinks resolved (on macOS the temporary directory is under /var -> /private/var).
  PROOT=$(cd -P "$ROOT" && pwd -P)
  # A stub rm that logs its arguments and removes nothing, for tests whose failure
  # mode would otherwise be an rm of / or of a directory outside the sandbox.
  printf '#!/bin/sh\nprintf "%%s\\n" "$*" >>"%s"\n' "$T/rm.log" >"$T/stub/rm"
  chmod +x "$T/stub/rm"
  : >"$T/rm.log"
}

use_stub_rm() {
  export PATH="$T/stub:$PATH"
}

@test "removes a tree inside ROOT and reports its physical path" {
  run_lab "$SAFERM" "$ROOT" "$ROOT/cache"
  [ "$status" -eq 0 ]
  [ "$output" = "removed: $PROOT/cache" ]
  [ ! -e "$ROOT/cache" ]
}

@test "the stub rm really records a call, so an empty log proves rm never ran" {
  use_stub_rm
  run_lab "$SAFERM" "$ROOT" "$ROOT/cache"
  log=$(cat "$T/rm.log")
  [ "$log" = "-rf -- ./cache" ]
}

@test "refuses an empty operand without running rm" {
  use_stub_rm
  run_lab "$SAFERM" "$ROOT" ''
  [ "$status" -eq 1 ]
  [ "$output" = "saferm: refusing '': empty operand" ]
  [ ! -s "$T/rm.log" ]
}

@test "refuses / and // without running rm" {
  use_stub_rm
  run_lab "$SAFERM" "$ROOT" / //
  [ "$status" -eq 1 ]
  [ "$output" = "saferm: refusing /: resolves to /
saferm: refusing //: resolves to /" ]
  [ ! -s "$T/rm.log" ]
}

@test "refuses ROOT itself in every spelling without running rm" {
  use_stub_rm
  run_lab "$SAFERM" "$ROOT" "$ROOT" "$ROOT/" "$ROOT/." "$ROOT/cache/.."
  [ "$status" -eq 1 ]
  [ "$output" = "saferm: refusing $ROOT: outside $PROOT
saferm: refusing $ROOT/: outside $PROOT
saferm: refusing $ROOT/.: ends in . or ..
saferm: refusing $ROOT/cache/..: ends in . or .." ]
  [ ! -s "$T/rm.log" ]
}

@test "refuses a dot-dot escape out of ROOT" {
  use_stub_rm
  run_lab "$SAFERM" "$ROOT" "$ROOT/cache/../../outside"
  [ "$status" -eq 1 ]
  [ "$output" = "saferm: refusing $ROOT/cache/../../outside: outside $PROOT" ]
  [ ! -s "$T/rm.log" ]
}

@test "refuses a path whose symlinked parent leaves ROOT" {
  ln -s "$T/outside" "$ROOT/esc"
  use_stub_rm
  run_lab "$SAFERM" "$ROOT" "$ROOT/esc/keep"
  [ "$status" -eq 1 ]
  [ "$output" = "saferm: refusing $ROOT/esc/keep: outside $PROOT" ]
  [ ! -s "$T/rm.log" ]
  [ -f "$T/outside/keep" ]
}

@test "a symlink operand with a trailing slash removes the link, not the target's contents" {
  ln -s "$T/outside" "$ROOT/link"
  run_lab "$SAFERM" "$ROOT" "$ROOT/link/"
  [ "$status" -eq 0 ]
  [ "$output" = "removed: $PROOT/link" ]
  [ ! -h "$ROOT/link" ]
  [ -f "$T/outside/keep" ]
}

@test "-n prints the plan and removes nothing" {
  run_lab "$SAFERM" -n "$ROOT" "$ROOT/cache"
  [ "$status" -eq 0 ]
  [ "$output" = "would remove: $PROOT/cache" ]
  [ -f "$ROOT/cache/sub/f" ]
}

@test "a relative operand that starts with a dash is removed as a file, not read as options" {
  : >"$ROOT/-rf"
  cd "$ROOT"
  run_lab "$SAFERM" . -rf
  [ "$status" -eq 0 ]
  [ "$output" = "removed: $PROOT/-rf" ]
  [ ! -e "$ROOT/-rf" ]
  [ -f "$ROOT/cache/sub/f" ]
}

@test "an absent target succeeds, so a rerun is idempotent" {
  run_lab "$SAFERM" "$ROOT" "$ROOT/cache"
  [ "$status" -eq 0 ]
  run_lab "$SAFERM" "$ROOT" "$ROOT/cache"
  [ "$status" -eq 0 ]
  [ "$output" = "saferm: absent: $PROOT/cache" ]
}

@test "a trailing newline in a directory name survives path resolution" {
  d="$ROOT/d"$'\n'
  mkdir "$d"
  : >"$d/x"
  run_lab "$SAFERM" "$ROOT" "$d/x"
  [ "$status" -eq 0 ]
  [ "$output" = "removed: $PROOT/d"$'\n'"/x" ]
  [ ! -e "$d/x" ]
  [ -d "$d" ]
}

@test "usage errors, an empty ROOT, and ROOT=/ exit 2 without running rm" {
  use_stub_rm
  run_lab "$SAFERM" "$ROOT"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: saferm.sh [-n] ROOT TARGET..." ]
  run_lab "$SAFERM" '' "$ROOT/cache"
  [ "$status" -eq 2 ]
  [ "$output" = "saferm: ROOT is empty" ]
  run_lab "$SAFERM" / /etc
  [ "$status" -eq 2 ]
  [ "$output" = "saferm: ROOT must not be /" ]
  [ ! -s "$T/rm.log" ]
  [ -f "$ROOT/cache/sub/f" ]
}
