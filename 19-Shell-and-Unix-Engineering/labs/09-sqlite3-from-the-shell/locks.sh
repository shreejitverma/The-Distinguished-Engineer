#!/bin/sh
# locks.sh upgrade DIR JOURNAL BEGIN | busy DIR
#   upgrade  connection A starts BEGIN (or BEGIN IMMEDIATE), reads, waits while connection B writes,
#            then A tries to write: show who loses and what the database holds afterwards
#   busy     connection A holds a write lock; show that a bare writer fails at once and that one
#            with `.timeout` waits for the lock instead
# Marker files order the processes; nothing depends on sleeping for a guessed time.
SQLITE3=${SQLITE3:-sqlite3}
here=$(dirname "$0")
waitfor=$here/waitfor.sh

upgrade() {
  dir=$1
  journal=$2
  begin=$3
  db=$dir/t.db
  "$SQLITE3" "$db" "pragma journal_mode=$journal" "create table t(a)" "insert into t values(1)" >/dev/null || return 1
  {
    printf '%s;\n' "$begin"
    printf 'select count(*) from t;\n'
    printf '.shell touch %s/a_read; /bin/sh %s %s/b_done\n' "$dir" "$waitfor" "$dir"
    printf 'insert into t values(100);\n'
    printf 'commit;\n'
  } >"$dir/a.sql"
  "$SQLITE3" "$db" <"$dir/a.sql" >"$dir/a.out" 2>"$dir/a.err" &
  apid=$!
  /bin/sh "$waitfor" "$dir/a_read" || return 1
  "$SQLITE3" "$db" '.timeout 300' 'insert into t values(200);' 2>"$dir/b.err"
  brc=$?
  touch "$dir/b_done"
  wait "$apid"
  arc=$?
  if grep -q 'database is locked' "$dir/a.err"; then alocked=yes; else alocked=no; fi
  if grep -q 'database is locked' "$dir/b.err"; then blocked=yes; else blocked=no; fi
  rows=$("$SQLITE3" "$db" 'select group_concat(a) from (select a from t order by rowid)')
  printf 'A_rc=%s A_locked=%s B_rc=%s B_locked=%s rows=%s\n' "$arc" "$alocked" "$brc" "$blocked" "$rows"
}

busy() {
  dir=$1
  db=$dir/t.db
  "$SQLITE3" "$db" "create table t(a)" >/dev/null || return 1
  {
    printf 'begin immediate;\n'
    printf 'insert into t values(1);\n'
    printf '.shell touch %s/a_locked; /bin/sh %s %s/release\n' "$dir" "$waitfor" "$dir"
    printf 'commit;\n'
  } >"$dir/a.sql"
  "$SQLITE3" "$db" <"$dir/a.sql" >"$dir/a.out" 2>"$dir/a.err" &
  apid=$!
  /bin/sh "$waitfor" "$dir/a_locked" || return 1
  "$SQLITE3" "$db" 'insert into t values(2)' 2>"$dir/b0.err"
  b0=$?
  {
    printf '.timeout 20000\n'
    printf '.shell touch %s/b1_started\n' "$dir"
    printf 'insert into t values(3);\n'
  } >"$dir/b1.sql"
  "$SQLITE3" "$db" <"$dir/b1.sql" >"$dir/b1.out" 2>"$dir/b1.err" &
  bpid=$!
  /bin/sh "$waitfor" "$dir/b1_started" || return 1
  /bin/sleep 1
  if kill -0 "$bpid" 2>/dev/null; then waiting=yes; else waiting=no; fi
  touch "$dir/release"
  wait "$apid"
  arc=$?
  wait "$bpid"
  b1=$?
  rows=$("$SQLITE3" "$db" 'select group_concat(a) from (select a from t order by rowid)')
  printf 'bare_writer_rc=%s timeout_writer_blocked_while_A_held=%s A_rc=%s timeout_writer_rc=%s rows=%s\n' \
    "$b0" "$waiting" "$arc" "$b1" "$rows"
}

case $1 in
  upgrade) upgrade "$2" "$3" "$4" ;;
  busy) busy "$2" ;;
  *)
    echo "usage: locks.sh upgrade DIR JOURNAL BEGIN | busy DIR" >&2
    exit 2
    ;;
esac
