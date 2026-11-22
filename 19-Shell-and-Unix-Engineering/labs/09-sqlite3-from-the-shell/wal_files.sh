#!/bin/sh
# wal_files.sh DIR: the -wal file lifecycle and the persistence of WAL mode, in one live session.
# Prints the size of DB-wal after a commit and after wal_checkpoint(truncate), then reopens the
# database in a new process to show the journal mode and the two file-format bytes of the header.
SQLITE3=${SQLITE3:-sqlite3}
dir=$1
db=$dir/w.db
here=$(dirname "$0")
header="select hex(substr(readfile('$db'), 19, 2))"
{
  printf 'pragma journal_mode=wal;\n'
  printf 'create table t(a);\n'
  printf 'insert into t values(1);\n'
  printf '.print wal_bytes_after_commit\n'
  printf '.shell /bin/sh %s/fsize.sh %s-wal\n' "$here" "$db"
  printf 'pragma wal_checkpoint(truncate);\n'
  printf '.print wal_bytes_after_truncate\n'
  printf '.shell /bin/sh %s/fsize.sh %s-wal\n' "$here" "$db"
} >"$dir/session.sql"
"$SQLITE3" "$db" <"$dir/session.sql" || exit 1
mode=$("$SQLITE3" "$db" 'pragma journal_mode')
wal_bytes=$("$SQLITE3" "$db" "$header")
printf 'reopened_journal_mode=%s\n' "$mode"
printf 'header_version_bytes_wal=%s\n' "$wal_bytes"
"$SQLITE3" "$db" 'pragma journal_mode=delete' >/dev/null || exit 1
delete_bytes=$("$SQLITE3" "$db" "$header")
printf 'header_version_bytes_delete=%s\n' "$delete_bytes"
