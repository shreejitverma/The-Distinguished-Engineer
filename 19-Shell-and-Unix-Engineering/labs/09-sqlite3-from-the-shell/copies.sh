#!/bin/sh
# copies.sh DIR: three ways to copy a live database (.dump piped back in, .backup, VACUUM INTO)
# and the check that each copy holds the same rows.
SQLITE3=${SQLITE3:-sqlite3}
dir=$1
src=$dir/src.db
"$SQLITE3" "$src" <<'SQL' || exit 1
create table t(id integer primary key, s text, b blob, r real);
insert into t values(1, 'it''s', x'00ff', 0.1), (2, null, x'', 1e300);
create index t_s on t(s);
SQL
rows='select id, quote(s), hex(b), r from t order by id'
want=$("$SQLITE3" "$src" "$rows")
"$SQLITE3" "$src" .dump | "$SQLITE3" "$dir/dump.db" || exit 1
"$SQLITE3" "$src" ".backup '$dir/backup.db'" || exit 1
"$SQLITE3" "$src" "vacuum into '$dir/vacuum.db'" || exit 1
for name in dump backup vacuum; do
  got=$("$SQLITE3" "$dir/$name.db" "$rows")
  check=$("$SQLITE3" "$dir/$name.db" 'pragma integrity_check')
  if [ "$got" = "$want" ]; then
    printf '%s rows=match integrity=%s\n' "$name" "$check"
  else
    printf '%s rows=DIFFER\n' "$name"
  fi
done
"$SQLITE3" "$src" "vacuum into '$dir/vacuum.db'" 2>/dev/null
printf 'vacuum_into_existing_rc=%s\n' "$?"
