#!/bin/sh
# import_csv.sh MODE CSV DB: what `.import --csv` does to one CSV under three table definitions.
#   auto    let .import create the table, report the declared type of zip and the stored value
#   typed   pre-create a STRICT table with TEXT columns, skip the header row, report the value
#   ragged  pre-create a STRICT table and import rows with too few and too many fields
SQLITE3=${SQLITE3:-sqlite3}
mode=$1
csv=$2
db=$3
case $mode in
  auto)
    "$SQLITE3" "$db" ".import --csv '$csv' t" || exit 1
    decl=$("$SQLITE3" "$db" "select type from pragma_table_info('t') where name = 'zip'")
    val=$("$SQLITE3" "$db" "select zip from t where name = 'Ann'")
    printf 'declared=[%s] zip=%s\n' "$decl" "$val"
    ;;
  typed)
    "$SQLITE3" "$db" "create table t(id integer, name text, zip text) strict" || exit 1
    "$SQLITE3" "$db" ".import --csv --skip 1 '$csv' t" || exit 1
    "$SQLITE3" "$db" "select name, zip, typeof(zip) from t order by id"
    ;;
  ragged)
    "$SQLITE3" "$db" "create table t(a text, b text, c text) strict" || exit 1
    "$SQLITE3" "$db" ".import --csv --skip 1 '$csv' t" 2>&1
    printf 'import_rc=%s\n' "$?"
    "$SQLITE3" "$db" "select quote(a), quote(b), quote(c) from t order by rowid"
    ;;
  *)
    echo "usage: import_csv.sh auto|typed|ragged CSV DB" >&2
    exit 2
    ;;
esac
