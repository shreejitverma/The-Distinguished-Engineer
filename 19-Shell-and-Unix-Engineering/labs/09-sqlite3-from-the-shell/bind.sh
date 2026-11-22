#!/bin/sh
# bind.sh MODE DB VALUE: look VALUE up in users.name by three ways of getting it into the SQL.
#   interpolate  splice VALUE into the SQL text between single quotes (the injectable way)
#   literal      double the single quotes and set it with `.parameter set` (breaks on " and backslash)
#   readfile     write VALUE to a file and read it as a blob inside SQL (no quoting of VALUE at all)
# Prints the matching names, one per line; a stray usage message shows up on the same stream.
SQLITE3=${SQLITE3:-sqlite3}
mode=$1
db=$2
value=$3
"$SQLITE3" "$db" <<'SQL' || exit 1
create table if not exists users(id integer primary key, name text);
delete from users;
insert into users(name) values('ann'), ('bob'), ('a\nb'), ('say "hi"');
SQL
case $mode in
  interpolate)
    "$SQLITE3" "$db" "select name from users where name = '$value' order by id"
    ;;
  literal)
    esc=$(printf '%s' "$value" | sed "s/'/''/g")
    "$SQLITE3" "$db" ".parameter init" ".parameter set :v \"'$esc'\"" \
      "select name from users where name = :v order by id" 2>&1
    ;;
  readfile)
    valfile=$db.value
    printf '%s' "$value" >"$valfile"
    "$SQLITE3" "$db" ".parameter init" ".parameter set :f \"'$valfile'\"" \
      "select name from users where name = cast(readfile(:f) as text) order by id"
    ;;
  *)
    echo "usage: bind.sh interpolate|literal|readfile DB VALUE" >&2
    exit 2
    ;;
esac
