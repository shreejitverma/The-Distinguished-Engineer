#!/usr/bin/env bats
# shellcheck disable=SC2312 # each test compares captured output, so a failing command already fails its comparison
# Idempotent scripts: a naive one-shot script against a converging one, crash-and-rerun at every
# crash point, and the utility and shell behaviors that make a rerun safe or unsafe.

setup() {
  load ../lib/lab
  CONVERGE="$BATS_TEST_DIRNAME/converge.sh"
  NAIVE="$BATS_TEST_DIRNAME/naive-provision.sh"
  ROOT="$BATS_TEST_TMPDIR/root"
}

# seed_root DIR: the starting state both scripts expect (hosts deliberately lacks a final newline).
seed_root() {
  mkdir -p "$1/skel" "$1/releases/v1" "$1/run"
  printf 'name=app\nport=80\n' >"$1/skel/app.conf"
  printf '127.0.0.1 localhost' >"$1/hosts"
  printf '4242\n' >"$1/run/stale.pid"
}

# tree_state DIR: every path under DIR, sorted, with its type and a checksum or link target.
tree_state() {
  local p sum
  (
    cd "$1" || exit 1
    find . | LC_ALL=C sort | while IFS= read -r p; do
      if [ -L "$p" ]; then
        printf '%s -> %s\n' "$p" "$(readlink "$p")"
      elif [ -d "$p" ]; then
        printf '%s/\n' "$p"
      else
        sum=$(cksum <"$p")
        printf '%s %s\n' "$p" "$sum"
      fi
    done
  )
}

# inode FILE: the inode number of FILE.
inode() {
  local line
  line=$(ls -di "$1")
  printf '%s\n' "${line%% *}"
}

@test "naive script: the second run exits 0 and corrupts the tree" {
  seed_root "$ROOT"
  run_lab "$NAIVE" "$ROOT"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
  [ "$(cat "$ROOT/conf/app.conf")" = "$(printf 'name=app\nport=8080')" ]

  run_lab "$NAIVE" "$ROOT"
  [ "$status" -eq 0 ]               # the rerun "succeeds" ...
  [[ "$output" == *stale.pid* ]]    # ... after rm complained on stderr
  [ -f "$ROOT/conf/skel/app.conf" ] # cp -R nested skel inside conf
  [ "$(cat "$ROOT/conf/app.conf")" = "$(printf 'name=app\nport=808080')" ]
  [ "$(cat "$ROOT/hosts")" = "$(printf '127.0.0.1 localhost10.0.0.1 db\n10.0.0.1 db')" ]
  [ -L "$ROOT/releases/v1/v1" ] # ln -s created a link inside the target directory
  [ "$(readlink "$ROOT/releases/v1/v1")" = releases/v1 ]
}

@test "converge: every step changes on a fresh root, nothing changes on a rerun" {
  seed_root "$ROOT"
  run_lab "$CONVERGE" "$ROOT"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf '%s\n' 'changed dir conf' 'changed line hosts' \
    'changed file conf/app.conf' 'changed link current' 'changed absent run/stale.pid' 'changed=5')" ]
  [ "$(cat "$ROOT/hosts")" = "$(printf '127.0.0.1 localhost\n10.0.0.1 db')" ]
  [ "$(cat "$ROOT/conf/app.conf")" = "$(printf 'name=app\nport=8080')" ]
  [ "$(readlink "$ROOT/current")" = releases/v1 ]
  first=$(tree_state "$ROOT")

  run_lab "$CONVERGE" "$ROOT"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf '%s\n' 'ok dir conf' 'ok line hosts' \
    'ok file conf/app.conf' 'ok link current' 'ok absent run/stale.pid' 'changed=0')" ]
  [ "$(tree_state "$ROOT")" = "$first" ]
}

@test "converge: a SIGKILL at every crash point, then a rerun, gives the clean-run tree" {
  seed_root "$BATS_TEST_TMPDIR/ref"
  run_lab "$CONVERGE" "$BATS_TEST_TMPDIR/ref"
  [ "$status" -eq 0 ]
  want=$(tree_state "$BATS_TEST_TMPDIR/ref")

  for point in after-dir line-newline after-line file-staged after-file link-removed after-link; do
    dir="$BATS_TEST_TMPDIR/crash-$point"
    seed_root "$dir"
    CONVERGE_CRASH=$point run_lab "$CONVERGE" "$dir"
    [ "$status" -eq 137 ] || {
      echo "no SIGKILL at $point: $status"
      false
    }
    [ "$(tree_state "$dir")" != "$want" ] || {
      echo "crash at $point left nothing partial"
      false
    }

    run_lab "$CONVERGE" "$dir"
    [ "$status" -eq 0 ]
    [ "$(tree_state "$dir")" = "$want" ] || {
      echo "rerun after $point diverged"
      false
    }

    run_lab "$CONVERGE" "$dir"
    [ "$status" -eq 0 ]
    [ "${lines[5]}" = "changed=0" ]
  done
}

@test "converge: the staged file from a crash is visible, then consumed by the rerun" {
  seed_root "$ROOT"
  CONVERGE_CRASH=file-staged run_lab "$CONVERGE" "$ROOT"
  [ "$status" -eq 137 ]
  [ -f "$ROOT/conf/.app.conf.new" ]
  [ ! -e "$ROOT/conf/app.conf" ]

  run_lab "$CONVERGE" "$ROOT"
  [ "$status" -eq 0 ]
  [ "${lines[2]}" = "changed file conf/app.conf" ]
  [ ! -e "$ROOT/conf/.app.conf.new" ]
}

@test "converge: an unchanged file keeps its inode; real drift is repaired and reported" {
  seed_root "$ROOT"
  run_lab "$CONVERGE" "$ROOT"
  [ "$status" -eq 0 ]
  before=$(inode "$ROOT/conf/app.conf")
  run_lab "$CONVERGE" "$ROOT"
  [ "$(inode "$ROOT/conf/app.conf")" = "$before" ]

  printf 'name=app\nport=9999\n' >"$ROOT/conf/app.conf" # someone edits the managed file
  run_lab "$CONVERGE" "$ROOT"
  [ "$status" -eq 0 ]
  [ "${lines[2]}" = "changed file conf/app.conf" ]
  [ "${lines[5]}" = "changed=1" ]
  [ "$(cat "$ROOT/conf/app.conf")" = "$(printf 'name=app\nport=8080')" ]
}

@test "converge: a failing render leaves the installed file intact and exits 1" {
  seed_root "$ROOT"
  run_lab "$CONVERGE" "$ROOT"
  [ "$status" -eq 0 ]
  rm "$ROOT/skel/app.conf"
  run_lab "$CONVERGE" "$ROOT"
  [ "$status" -eq 1 ]
  [[ "$output" == *"converge: cannot render conf/app.conf"* ]]
  [ "$(cat "$ROOT/conf/app.conf")" = "$(printf 'name=app\nport=8080')" ]
}

@test "converge: a real directory where the symlink belongs is refused, not nested into" {
  seed_root "$ROOT"
  mkdir "$ROOT/current"
  run_lab "$CONVERGE" "$ROOT"
  [ "$status" -eq 1 ]
  [[ "$output" == *"converge: current exists and is not a symbolic link"* ]]
  [ -z "$(ls -A "$ROOT/current")" ]
}

@test "ln -s and ln -sf over a link to a directory nest the new link; ln -sfn replaces it" {
  mkdir -p "$BATS_TEST_TMPDIR/r/v1" "$BATS_TEST_TMPDIR/r/v2"
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'cd "$1" || exit 9
    ln -s r/v1 cur || exit 1
    ln -s r/v2 cur || exit 2
    ln -sf r/v2 cur || exit 3
    echo "cur=$(readlink cur) nested=$(readlink r/v1/v2)"
    ln -sfn r/v2 cur || exit 4
    echo "cur=$(readlink cur)"' sh "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "cur=r/v1 nested=r/v2" ]
  [ "${lines[1]}" = "cur=r/v2" ]
}

@test "cp -R into an existing directory copies the source inside it" {
  mkdir -p "$BATS_TEST_TMPDIR/skel"
  printf 'x\n' >"$BATS_TEST_TMPDIR/skel/f"
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'cd "$1" || exit 9
    cp -R skel conf && cp -R skel conf || exit 1
    find conf -type f | LC_ALL=C sort' sh "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf '%s\n' conf/f conf/skel/f)" ]
}

@test "mkdir -p accepts an existing directory and rejects an existing file" {
  mkdir -p "$BATS_TEST_TMPDIR/d"
  printf 'x\n' >"$BATS_TEST_TMPDIR/f"
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'cd "$1" || exit 9
    mkdir -p d; echo "dir=$?"
    mkdir d 2>/dev/null; echo "plain=$?"
    mkdir -p f 2>/dev/null; echo "file=$?"' sh "$BATS_TEST_TMPDIR"
  [ "${lines[0]}" = "dir=0" ]
  [ "${lines[1]}" != "plain=0" ]
  [ "${lines[2]}" != "file=0" ]
  [ "${#lines[@]}" -eq 3 ]
}

@test "rm -f is silent and exits 0 for a missing operand and for no operands" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'cd "$1" || exit 9
    rm -f missing-file; echo "missing=$?"
    rm -f; echo "none=$?"
    rm missing-file 2>/dev/null; echo "plain=$?"' sh "$BATS_TEST_TMPDIR"
  [ "${lines[0]}" = "missing=0" ]
  [ "${lines[1]}" = "none=0" ]
  [ "${lines[2]}" != "plain=0" ]
  [ "${#lines[@]}" -eq 3 ]
}

@test "set -C: a second > to a regular file fails and keeps it; >| and /dev/null still work" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'cd "$1" || exit 9
    set -C
    echo first >f
    if { echo second >f; } 2>/dev/null; then echo clobbered; else echo refused; fi
    cat f
    echo third >|f
    cat f
    if echo x >/dev/null; then echo devnull-ok; else echo devnull-refused; fi' sh "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 4 ]
  [ "${lines[0]}" = refused ]
  [ "${lines[1]}" = first ]
  [ "${lines[2]}" = third ]
  [ "${lines[3]}" = devnull-ok ]
}

@test "set -C: a failed redirection on the special built-in : exits a POSIX shell" {
  printf 'x\n' >"$BATS_TEST_TMPDIR/f"
  family=$(lab_shell_family)
  if [ "$family" = bash ]; then
    # shellcheck disable=SC2016 # the expansion belongs to the shell under test
    run_lab_c 'case :${SHELLOPTS-}: in *:posix:*) echo posix ;; *) echo normal ;; esac'
    family="bash-$output"
  elif [ "$family" = yash ]; then
    # shellcheck disable=SC2016 # the expansion belongs to the shell under test
    run_lab_c 'if set -o | grep -q "^posixlycorrect *on"; then echo posix; else echo normal; fi'
    family="yash-$output"
  fi
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'set -C; : >"$1"; echo survived' sh "$BATS_TEST_TMPDIR/f"
  [ "$(cat "$BATS_TEST_TMPDIR/f")" = x ]
  case $family in
    bash-normal | zsh | yash-normal) # bash outside POSIX mode; native zsh (POSIX_BUILTINS off); yash outside posixlycorrect
      [ "$status" -eq 0 ]
      [[ "$output" == *survived ]]
      ;;
    bash-posix | zsh-sh | yash-posix | dash | ksh93 | mksh | posh | busybox)
      [ "$status" -ne 0 ]
      [[ "$output" != *survived* ]]
      ;;
    *) false ;;
  esac
}
