#!/bin/sh
# formats.sh DB MODE: render one awkward row (a pipe, a newline, a comma and quotes) in an output mode.
# MODE is one of list, csv, json, ascii: the sqlite3 option -MODE is applied before the query.
SQLITE3=${SQLITE3:-sqlite3}
db=$1
mode=$2
"$SQLITE3" "$db" <<'SQL' || exit 1
create table if not exists t(a, b, c);
delete from t;
insert into t values('a|b', 'x' || char(10) || 'y', 'say "hi", ok');
SQL
"$SQLITE3" "-$mode" "$db" 'select a, b, c from t'
