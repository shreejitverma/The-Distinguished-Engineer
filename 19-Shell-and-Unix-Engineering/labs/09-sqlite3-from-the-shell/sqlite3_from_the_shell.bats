#!/usr/bin/env bats
# sqlite3_from_the_shell.bats: the sqlite3 CLI as a Unix filter - exit status, statement and
# argument handling, output formats that stay parseable, .import typing, SQL injection through
# shell quoting, two-process locking, WAL file lifecycle, and copying a live database.
# The shell under test only ever builds argv and stdin for sqlite3; every behavior asserted here
# belongs to the sqlite3 binary found on PATH (or named by $SQLITE3), so no test branches on the
# shell family.
# shellcheck disable=SC2016 # every run_lab_c string is expanded by the shell under test

setup() {
  load ../lib/lab
  LAB_DIR=$BATS_TEST_DIRNAME
  SQLITE3=${SQLITE3:-sqlite3}
  export SQLITE3
  command -v "$SQLITE3" >/dev/null 2>&1 || {
    echo "sqlite3 is not installed: $SQLITE3" >&2
    return 1
  }
  W=$BATS_TEST_TMPDIR
}

# --- exit status and statement handling --------------------------------------------------

@test "stdin script keeps running after a failing statement and exits 1" {
  run_lab_c 'printf "select 1;\nselec 2;\nselect 3;\n" | "$1" :memory: 2>/dev/null; echo "rc=$?"' inline "$SQLITE3"
  [ "$status" -eq 0 ]
  [ "$output" = $'1\n3\nrc=1' ]
}

@test "-bail stops a stdin script at the first failing statement" {
  run_lab_c 'printf "select 1;\nselec 2;\nselect 3;\n" | "$1" -bail :memory: 2>/dev/null; echo "rc=$?"' inline "$SQLITE3"
  [ "$status" -eq 0 ]
  [ "$output" = $'1\nrc=1' ]
}

@test "statements given as arguments stop at the first failing argument" {
  run_lab_c '"$1" :memory: "select 1;" "selec 2;" "select 3;" 2>/dev/null; echo "rc=$?"' inline "$SQLITE3"
  [ "$status" -eq 0 ]
  [ "$output" = $'1\nrc=1' ]
}

@test "a mistyped database name creates an empty file, and -readonly refuses to" {
  run_lab_c 'cd "$2" || exit 9
    "$1" typo.db "select 1" >/dev/null
    if [ -f typo.db ] && [ ! -s typo.db ]; then echo typo-created-empty; else echo typo-not-created; fi
    "$1" -readonly nofile.db "select 1" >/dev/null 2>&1
    echo "readonly_rc=$?"
    if [ -e nofile.db ]; then echo nofile-created; else echo nofile-absent; fi' inline "$SQLITE3" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = $'typo-created-empty\nreadonly_rc=1\nnofile-absent' ]
}

@test ".once redirects only the next SQL statement" {
  run_lab_c 'cd "$2" || exit 9
    "$1" :memory: ".once o.txt" "select 1;" "select 2;"
    cat o.txt' inline "$SQLITE3" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = $'2\n1' ]
}

@test "-init FILE runs before the arguments and decides the output mode" {
  printf '.mode csv\n.headers on\n' >"$W/rc.sql"
  run_lab_c '{ "$1" -init "$2" :memory: "select 1 as a, 2 as b;"
    "$1" -init /dev/null :memory: "select 1 as a, 2 as b;"; } | tr -d "\r"' inline "$SQLITE3" "$W/rc.sql"
  [ "$status" -eq 0 ]
  [ "$output" = $'a,b\n1,2\n1|2' ]
}

@test "-safe refuses .shell while a plain run executes it" {
  run_lab_c 'cd "$2" || exit 9
    "$1" :memory: ".shell touch marker" >/dev/null 2>&1
    echo "plain_rc=$?"
    if [ -e marker ]; then echo plain_ran; else echo plain_skipped; fi
    rm -f marker
    "$1" -safe :memory: ".shell touch marker" >/dev/null 2>&1
    echo "safe_rc=$?"
    if [ -e marker ]; then echo safe_ran; else echo safe_refused; fi' inline "$SQLITE3" "$W"
  [ "$status" -eq 0 ]
  [ "$output" = $'plain_rc=0\nplain_ran\nsafe_rc=1\nsafe_refused' ]
}

# --- output formats ----------------------------------------------------------------------

@test "list mode: a pipe and a newline inside the data look like separators" {
  run_lab "$LAB_DIR/formats.sh" "$W/f.db" list
  [ "$status" -eq 0 ]
  [ "$output" = $'a|b|x\ny|say "hi", ok' ]
}

@test "csv mode: RFC 4180 quoting and a CRLF row terminator keep the row recoverable" {
  run_lab "$LAB_DIR/formats.sh" "$W/f.db" csv
  [ "$status" -eq 0 ]
  [ "$output" = $'a|b,"x\ny","say ""hi"", ok"\r' ]
}

@test "json mode: one object per row with escapes" {
  run_lab "$LAB_DIR/formats.sh" "$W/f.db" json
  [ "$status" -eq 0 ]
  [ "$output" = '[{"a":"a|b","b":"x\ny","c":"say \"hi\", ok"}]' ]
}

@test "ascii mode: unit and record separators keep fields with pipes and newlines unambiguous" {
  run_lab "$LAB_DIR/formats.sh" "$W/f.db" ascii
  [ "$status" -eq 0 ]
  [ "$output" = $'a|b\x1fx\ny\x1fsay "hi", ok\x1e' ]
}

# --- .import and typing ------------------------------------------------------------------

@test ".import into a new table: the declared type of the created column decides whether 00501 survives" {
  printf 'id,name,zip\n1,Ann,00501\n2,Bob,10001\n' >"$W/in.csv"
  run_lab "$LAB_DIR/import_csv.sh" auto "$W/in.csv" "$W/auto.db"
  [ "$status" -eq 0 ]
  case $output in
    'declared=[] zip=00501') ;;
    'declared=[TEXT] zip=00501') ;;
    'declared=[ANY] zip=501') ;;
    *) false ;;
  esac
}

@test ".import into a pre-created STRICT TEXT table keeps leading zeros and skips the header" {
  printf 'id,name,zip\n1,Ann,00501\n2,Bob,10001\n' >"$W/in.csv"
  run_lab "$LAB_DIR/import_csv.sh" typed "$W/in.csv" "$W/typed.db"
  [ "$status" -eq 0 ]
  [ "$output" = $'Ann|00501|text\nBob|10001|text' ]
}

@test ".import pads short rows with NULL, drops extra fields, warns on stderr, and still exits 0" {
  printf 'a,b,c\n1,2\n3,4,5,6\n7,8,9\n' >"$W/rag.csv"
  run_lab "$LAB_DIR/import_csv.sh" ragged "$W/rag.csv" "$W/rag.db"
  [ "$status" -eq 0 ]
  [[ ${lines[0]} == *':2: expected 3 columns but found 2 - filling the rest with NULL' ]]
  [[ ${lines[1]} == *':3: expected 3 columns but found 4 - extras ignored' ]]
  [ "${lines[2]}" = 'import_rc=0' ]
  [ "${lines[3]}" = "'1'|'2'|NULL" ]
  [ "${lines[4]}" = "'3'|'4'|'5'" ]
  [ "${lines[5]}" = "'7'|'8'|'9'" ]
  [ "${#lines[@]}" -eq 6 ]
}

@test "an integer column accepts text it cannot convert, a STRICT table refuses it" {
  run_lab_c '"$1" :memory: "$2" "$3"' inline "$SQLITE3" \
    "create table t(a integer, b text); insert into t values('abc', 7);" \
    "select typeof(a), typeof(b) from t;"
  [ "$status" -eq 0 ]
  [ "$output" = 'text|text' ]
  run_lab_c '"$1" :memory: "$2" 2>&1; echo "rc=$?"' inline "$SQLITE3" \
    "create table t(a integer) strict; insert into t values('abc');"
  [ "$status" -eq 0 ]
  [[ $output == *'cannot store TEXT value in INTEGER column'* ]]
  [[ $output == *'rc=1' ]]
}

@test "NUL in a string: length() stops at it, hex() sees all of it" {
  run_lab_c '"$1" :memory: "$2"' inline "$SQLITE3" \
    "select length('a'||char(0)||'b'), hex('a'||char(0)||'b');"
  [ "$status" -eq 0 ]
  [ "$output" = '1|610062' ]
}

# --- defaults that are per connection ----------------------------------------------------

@test "foreign key enforcement is a per-connection setting that no other process inherits" {
  run_lab_c 'd=$("$1" :memory: "pragma foreign_keys")
    o=$((1 - d))
    "$1" "$2" "pragma foreign_keys=$o" >/dev/null
    r=$("$1" "$2" "pragma foreign_keys")
    echo "default=$d later_process=$r"' inline "$SQLITE3" "$W/fk.db"
  [ "$status" -eq 0 ]
  [[ $output =~ ^default=([01])\ later_process=([01])$ ]]
  [ "${BASH_REMATCH[1]}" = "${BASH_REMATCH[2]}" ]
}

@test "foreign keys are enforced only when the connection turns them on" {
  local schema="create table p(id integer primary key); create table c(p references p(id));"
  run_lab_c '"$1" :memory: "$2" 2>&1; echo "rc=$?"' inline "$SQLITE3" \
    "pragma foreign_keys=off; $schema insert into c values(99);"
  [ "$output" = 'rc=0' ]
  run_lab_c '"$1" :memory: "$2" 2>&1; echo "rc=$?"' inline "$SQLITE3" \
    "pragma foreign_keys=on; $schema insert into c values(99);"
  [[ $output == *'FOREIGN KEY constraint failed'* ]]
  [[ $output == *'rc=1' ]]
}

@test "double-quoted strings: a missing column becomes a string exactly when dqs_dml reads on" {
  run_lab_c '"$1" :memory: ".dbconfig dqs_dml" "select \"nosuchcol\";" 2>&1; echo "rc=$?"' inline "$SQLITE3"
  [ "$status" -eq 0 ]
  if [[ $output == *'dqs_dml on'* ]]; then
    [[ $output == *$'nosuchcol\nrc=0' ]]
  else
    [[ $output == *'dqs_dml off'* ]]
    [[ $output == *'no such column'* ]]
    [[ $output == *'rc=1' ]]
  fi
  run_lab_c '"$1" :memory: ".dbconfig dqs_dml off" "select \"nosuchcol\";" 2>&1; echo "rc=$?"' inline "$SQLITE3"
  [[ $output == *'no such column'* ]]
  [[ $output == *'rc=1' ]]
}

# --- SQL built from shell values ---------------------------------------------------------

@test "a quoted here-document hands \$ and backticks to sqlite3 untouched" {
  cat >"$W/heredoc.sh" <<'SCRIPT'
#!/bin/sh
"$1" :memory: <<'SQL'
select '$HOME', '`x`';
SQL
SCRIPT
  chmod +x "$W/heredoc.sh"
  run_lab "$W/heredoc.sh" "$SQLITE3"
  [ "$status" -eq 0 ]
  [ "$output" = '$HOME|`x`' ]
}

@test "splicing a value into the SQL text is injectable" {
  run_lab "$LAB_DIR/bind.sh" interpolate "$W/b.db" "nobody' or '1'='1"
  [ "$status" -eq 0 ]
  [ "$output" = $'ann\nbob\na\\nb\nsay "hi"' ]
}

@test "doubling the quotes and setting a parameter stops that injection" {
  run_lab "$LAB_DIR/bind.sh" literal "$W/b.db" "nobody' or '1'='1"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test ".parameter set silently misses a value with a backslash and fails on one with a double quote" {
  run_lab "$LAB_DIR/bind.sh" literal "$W/b.db" 'a\nb'
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  run_lab "$LAB_DIR/bind.sh" literal "$W/b.db" 'say "hi"'
  [ "$status" -eq 0 ]
  [[ $output == *'.parameter CMD ...'*'set PARAMETER VALUE'* ]]
  if printf '%s\n' "$output" | grep -qx 'say "hi"'; then
    false
  fi
}

@test "reading the value from a file with readfile() binds any bytes and keeps the injection inert" {
  run_lab "$LAB_DIR/bind.sh" readfile "$W/b.db" "nobody' or '1'='1"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  run_lab "$LAB_DIR/bind.sh" readfile "$W/b.db" 'a\nb'
  [ "$output" = 'a\nb' ]
  run_lab "$LAB_DIR/bind.sh" readfile "$W/b.db" 'say "hi"'
  [ "$output" = 'say "hi"' ]
}

@test "a hex literal built with od carries any bytes into SQL as text" {
  run_lab_c 'h=$(printf "%s" "$2" | od -An -tx1 | tr -d " \n")
    "$1" :memory: "select cast(x'"'"'$h'"'"' as text);"' inline "$SQLITE3" "x' or 1=1 --"
  [ "$status" -eq 0 ]
  [ "$output" = "x' or 1=1 --" ]
}

# --- two processes and one file ----------------------------------------------------------

@test "WAL, deferred BEGIN: the reader that upgrades loses with database is locked and its write is gone" {
  mkdir "$W/wal-deferred"
  run_lab "$LAB_DIR/locks.sh" upgrade "$W/wal-deferred" wal begin
  [ "$status" -eq 0 ]
  [ "$output" = 'A_rc=1 A_locked=yes B_rc=0 B_locked=no rows=1,200' ]
}

@test "WAL, BEGIN IMMEDIATE: the second writer is shut out and the first one's write survives" {
  mkdir "$W/wal-immediate"
  run_lab "$LAB_DIR/locks.sh" upgrade "$W/wal-immediate" wal 'begin immediate'
  [ "$status" -eq 0 ]
  [ "$output" = 'A_rc=0 A_locked=no B_rc=1 B_locked=yes rows=1,100' ]
}

@test "rollback journal, deferred BEGIN: an open reader blocks the writer's commit" {
  mkdir "$W/delete-deferred"
  run_lab "$LAB_DIR/locks.sh" upgrade "$W/delete-deferred" delete begin
  [ "$status" -eq 0 ]
  [ "$output" = 'A_rc=0 A_locked=no B_rc=1 B_locked=yes rows=1,100' ]
}

@test "busy: a bare writer fails at once, one with .timeout waits for the lock and then succeeds" {
  mkdir "$W/busy"
  run_lab "$LAB_DIR/locks.sh" busy "$W/busy"
  [ "$status" -eq 0 ]
  [ "$output" = 'bare_writer_rc=1 timeout_writer_blocked_while_A_held=yes A_rc=0 timeout_writer_rc=0 rows=1,3' ]
}

# --- WAL files and copies ----------------------------------------------------------------

@test "WAL mode: the -wal file grows with commits, truncates on checkpoint, and the mode persists in the header" {
  mkdir "$W/walfiles"
  run_lab "$LAB_DIR/wal_files.sh" "$W/walfiles"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = 'wal' ]
  [ "${lines[1]}" = 'wal_bytes_after_commit' ]
  [ "${lines[2]}" -gt 0 ]
  [[ ${lines[3]} =~ ^0\|([0-9]+)\|([0-9]+)$ ]]
  [ "${BASH_REMATCH[1]}" = "${BASH_REMATCH[2]}" ]
  [ "${lines[4]}" = 'wal_bytes_after_truncate' ]
  [ "${lines[5]}" = '0' ]
  [ "${lines[6]}" = 'reopened_journal_mode=wal' ]
  [ "${lines[7]}" = 'header_version_bytes_wal=0202' ]
  [ "${lines[8]}" = 'header_version_bytes_delete=0101' ]
  [ "${#lines[@]}" -eq 9 ]
}

@test "dump-and-pipe, .backup and VACUUM INTO all reproduce the rows; VACUUM INTO will not overwrite" {
  mkdir "$W/copies"
  run_lab "$LAB_DIR/copies.sh" "$W/copies"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = 'dump rows=match integrity=ok' ]
  [ "${lines[1]}" = 'backup rows=match integrity=ok' ]
  [ "${lines[2]}" = 'vacuum rows=match integrity=ok' ]
  [ "${lines[3]}" = 'vacuum_into_existing_rc=1' ]
  [ "${#lines[@]}" -eq 4 ]
}
