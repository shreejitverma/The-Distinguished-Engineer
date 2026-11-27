#!/usr/bin/env bats
# Characterization tests for a legacy script: the sandbox harness (characterize.sh) records what
# legacy-report.sh really does, golden transcripts pin it, and the tests below prove that the
# harness notices a change, survives the script's nondeterminism, and that the one seam added to the
# script (a source guard) does not change behavior.

setup() {
  load ../lib/lab
  HERE=$BATS_TEST_DIRNAME
  LEGACY=$HERE/legacy-report.sh
  SEAM=$HERE/legacy-report-seam.sh
  CHAR_ROOT=$BATS_TEST_TMPDIR
  export CHAR_ROOT
  cd "$BATS_TEST_TMPDIR" || return 1
}

# fixture NAME: build $BATS_TEST_TMPDIR/fx/NAME/logs with the files that NAME describes.
fixture() {
  local dir=$BATS_TEST_TMPDIR/fx/$1/logs
  mkdir -p "$dir"
  case $1 in
    basic)
      printf 'a\nb\nc\n' >"$dir/app.log"
      printf 'x\ny\n' >"$dir/db.log"
      ;;
    spaces)
      printf 'a\n' >"$dir/app.log"
      printf 'p\nq\n' >"$dir/my file.log"
      ;;
    empty) ;;
    *) return 1 ;;
  esac
}

# char [HARNESS OPTIONS] SCRIPT [ARG...]: run the harness under the shell under test, and make the
# script run under that same shell. Native zsh reads /etc/zshenv for every invocation, which can
# rewrite PATH and bypass the stub directory, so it runs with -f (NO_RCS).
char() {
  case $(lab_shell_family) in
    zsh) CHAR_SHELL="${LAB_SHELL:-sh} -f" ;;
    *) CHAR_SHELL=${LAB_SHELL:-sh} ;;
  esac
  export CHAR_SHELL
  run_lab "$HERE/characterize.sh" "$@"
}

# assert_golden NAME: the transcript of the last `run` equals golden/NAME; show the diff if not.
assert_golden() {
  local expected
  expected=$(cat "$HERE/golden/$1")
  if [ "$output" != "$expected" ]; then
    printf 'transcript differs from golden/%s:\n' "$1" >&2
    diff -u "$HERE/golden/$1" <(printf '%s\n' "$output") >&2 || true
    return 1
  fi
}

@test "a normal run is pinned end to end: status, stdout, report file, leaked temp file, no mail" {
  fixture basic
  char -f "$BATS_TEST_TMPDIR/fx/basic" -n "$HERE/normalize.sed" "$LEGACY" logs
  [ "$status" -eq 0 ]
  assert_golden basic.txt
  # the leaked temp file is part of the pinned behavior
  [[ $output == *"== tmp/report.<PID>.tmp"* ]] || false
}

@test "an alert run records exactly one mail call with its arguments and body" {
  fixture basic
  char -f "$BATS_TEST_TMPDIR/fx/basic" -e THRESHOLD=3 -n "$HERE/normalize.sed" "$LEGACY" logs
  assert_golden over-threshold.txt
  [[ $output == *$'--- calls\nmail <-s> <log alert> <ops@example.invalid>\n  | total=5' ]] || false
}

@test "the threshold boundary is pinned: a total equal to THRESHOLD sends no mail" {
  fixture basic
  char -f "$BATS_TEST_TMPDIR/fx/basic" -e THRESHOLD=5 -n "$HERE/normalize.sed" "$LEGACY" logs
  assert_golden boundary.txt
  [[ $output == *$'--- calls' ]] || false
}

@test "no argument: status 2 and the script's own usage message" {
  fixture basic
  char -f "$BATS_TEST_TMPDIR/fx/basic" -n "$HERE/normalize.sed" "$LEGACY"
  assert_golden usage.txt
}

@test "a missing directory: status 1, the script's own message, nothing written" {
  fixture basic
  char -f "$BATS_TEST_TMPDIR/fx/basic" -n "$HERE/normalize.sed" "$LEGACY" nope
  assert_golden missing-dir.txt
}

@test "a file name with a blank is split into two skipped names, in every shell (a pinned bug)" {
  fixture spaces
  char -f "$BATS_TEST_TMPDIR/fx/spaces" -n "$HERE/normalize.sed" "$LEGACY" logs
  assert_golden spaces.txt
  [[ $output == *$'logs/app.log 1\ntotal 1\nskipped 2'* ]] || false
}

@test "an empty directory: native zsh adds a NOMATCH diagnostic, every other shell stays silent" {
  fixture empty
  char -f "$BATS_TEST_TMPDIR/fx/empty" -n "$HERE/normalize.sed" "$LEGACY" logs
  case $(lab_shell_family) in
    zsh) assert_golden empty-zsh.txt ;;
    bash | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_golden empty.txt ;;
    *) false ;;
  esac
}

@test "the clock and the pid make two raw transcripts differ; normalization makes them equal" {
  fixture basic
  char -f "$BATS_TEST_TMPDIR/fx/basic" "$LEGACY" logs
  first=$output
  char -f "$BATS_TEST_TMPDIR/fx/basic" "$LEGACY" logs
  second=$output
  [ "$first" != "$second" ]
  char -f "$BATS_TEST_TMPDIR/fx/basic" -n "$HERE/normalize.sed" "$LEGACY" logs
  first=$output
  char -f "$BATS_TEST_TMPDIR/fx/basic" -n "$HERE/normalize.sed" "$LEGACY" logs
  second=$output
  [ "$first" = "$second" ]
}

@test "the sandbox hides the caller's environment, and PATH stubs catch only bare command names" {
  USER=outsider CHAR_LEAK=visible char "$HERE/probe-env.sh"
  assert_golden probe-env.txt
}

@test "a missing command gives status 127 and a shell-specific diagnostic that normalization can pin" {
  char "$HERE/probe-diag.sh"
  [ "$status" -eq 0 ]
  [[ $output == *$'--- stdout\nrc=127\n--- stderr\n'*nosuchcmd-xyz*$'\n--- files'* ]] || false
  char -n "$HERE/normalize-diag.sed" "$HERE/probe-diag.sh"
  assert_golden probe-diag.txt
}

@test "sourcing the guarded copy defines the functions without running main" {
  fixture basic
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'LEGACY_SOURCE_ONLY=1; . "$1"; summarize "$2"' sh "$SEAM" "$BATS_TEST_TMPDIR/fx/basic/logs"
  [ "$status" -eq 0 ]
  [ "$output" = "$BATS_TEST_TMPDIR/fx/basic/logs/app.log 3
$BATS_TEST_TMPDIR/fx/basic/logs/db.log 2
total 5
skipped 0" ]
  [ ! -e report.txt ]
}

@test "sourcing the unguarded script runs main, and its exit ends the sourcing shell" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'f=$1; set --; . "$f"; echo survived' sh "$LEGACY"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: legacy-report.sh DIR" ]
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'f=$1; set --; . "$f"; echo survived' sh "$SEAM"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: legacy-report.sh DIR" ]
}

@test "a function that calls exit must be called in a subshell, or it ends the test shell" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'f=$1; LEGACY_SOURCE_ONLY=1; . "$f"; set --; ( main ); echo "rc=$?"' sh "$SEAM"
  [ "$status" -eq 0 ]
  [ "$output" = "usage: legacy-report.sh DIR
rc=2" ]
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'f=$1; LEGACY_SOURCE_ONLY=1; . "$f"; set --; main; echo "rc=$?"' sh "$SEAM"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: legacy-report.sh DIR" ]
}

@test "the seam is the only edit: the guarded copy differs from the original in the last line alone" {
  local lines keep last guard
  lines=$(wc -l <"$LEGACY")
  lines=${lines// /}
  keep=$((lines - 1))
  head -n "$keep" "$LEGACY" >"$BATS_TEST_TMPDIR/legacy.head"
  head -n "$keep" "$SEAM" >"$BATS_TEST_TMPDIR/seam.head"
  cmp "$BATS_TEST_TMPDIR/legacy.head" "$BATS_TEST_TMPDIR/seam.head"
  last=$(tail -n 1 "$LEGACY")
  guard=$(tail -n 3 "$SEAM")
  # shellcheck disable=SC2016 # the script text is compared literally
  [ "$last" = 'main "$@"' ]
  # shellcheck disable=SC2016 # the script text is compared literally
  [ "$guard" = 'if [ -z "${LEGACY_SOURCE_ONLY-}" ]; then
  main "$@"
fi' ]
}

@test "adding the seam changes no transcript on any corpus case" {
  local name
  fixture basic
  fixture spaces
  fixture empty
  for name in basic spaces empty; do
    char -f "$BATS_TEST_TMPDIR/fx/$name" -n "$HERE/normalize.sed" "$LEGACY" logs
    first=$output
    char -f "$BATS_TEST_TMPDIR/fx/$name" -n "$HERE/normalize.sed" "$SEAM" logs
    second=$output
    [ "$first" = "$second" ]
  done
  char -f "$BATS_TEST_TMPDIR/fx/basic" -e THRESHOLD=3 -n "$HERE/normalize.sed" "$LEGACY" logs
  first=$output
  char -f "$BATS_TEST_TMPDIR/fx/basic" -e THRESHOLD=3 -n "$HERE/normalize.sed" "$SEAM" logs
  [ "$first" = "$output" ]
  char -f "$BATS_TEST_TMPDIR/fx/basic" -n "$HERE/normalize.sed" "$LEGACY" nope
  first=$output
  char -f "$BATS_TEST_TMPDIR/fx/basic" -n "$HERE/normalize.sed" "$SEAM" nope
  [ "$first" = "$output" ]
}

@test "a boundary mutant is caught only by the boundary case" {
  local count golden
  fixture basic
  sed 's/ -gt / -ge /' "$LEGACY" >"$BATS_TEST_TMPDIR/mutant.sh"
  count=$(grep -c -e "-ge" "$BATS_TEST_TMPDIR/mutant.sh")
  [ "$count" -eq 1 ]
  chmod +x "$BATS_TEST_TMPDIR/mutant.sh"
  # off the boundary (total 5, threshold 3) original and mutant agree: the corpus is blind here
  char -f "$BATS_TEST_TMPDIR/fx/basic" -e THRESHOLD=3 -n "$HERE/normalize.sed" "$LEGACY" logs
  first=$output
  char -f "$BATS_TEST_TMPDIR/fx/basic" -e THRESHOLD=3 -n "$HERE/normalize.sed" "$BATS_TEST_TMPDIR/mutant.sh" logs
  [ "$first" = "$output" ]
  # on the boundary (total 5, threshold 5) the mutant mails and the transcript shows it
  char -f "$BATS_TEST_TMPDIR/fx/basic" -e THRESHOLD=5 -n "$HERE/normalize.sed" "$BATS_TEST_TMPDIR/mutant.sh" logs
  golden=$(cat "$HERE/golden/boundary.txt")
  [ "$output" != "$golden" ]
  [[ $output == *$'--- calls\nmail <-s> <log alert> <ops@example.invalid>\n  | total=5' ]] || false
}

@test "an added side effect appears in the files section" {
  local count golden
  fixture basic
  sed 's/^  cat report.txt$/  cat report.txt; : >stray.txt/' "$LEGACY" >"$BATS_TEST_TMPDIR/mutant.sh"
  count=$(grep -c -e "stray.txt" "$BATS_TEST_TMPDIR/mutant.sh")
  [ "$count" -eq 1 ]
  chmod +x "$BATS_TEST_TMPDIR/mutant.sh"
  char -f "$BATS_TEST_TMPDIR/fx/basic" -n "$HERE/normalize.sed" "$BATS_TEST_TMPDIR/mutant.sh" logs
  golden=$(cat "$HERE/golden/basic.txt")
  [ "$output" != "$golden" ]
  [[ $output == *"== work/stray.txt"* ]] || false
}
