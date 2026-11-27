#!/usr/bin/env bats
# Tests for xsv/qsv indexing, RFC 4180 parsing, and Unix line-tool divergences.

setup() {
  load ../lib/lab
  LAB_DIR="$BATS_TEST_DIRNAME"
  PROBE="$LAB_DIR/xsv_probe.sh"
  CC_BIN="${CC:-cc}"

  # Ensure csv_indexer binary is compiled
  if [ ! -x "$LAB_DIR/csv_indexer" ]; then
    "$CC_BIN" -O2 -Wall -Wextra -Werror "$LAB_DIR/csv_indexer.c" -o "$LAB_DIR/csv_indexer"
  fi
}

@test "naive line count fails on multi-line quoted CSV records" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/multiline.csv"
id,description,val
1,"Line one
Line two",100
2,"Single line",200
EOF

  # Physical line count is 4 due to embedded newline
  run_lab "$PROBE" naive-count "$BATS_TEST_TMPDIR/multiline.csv"
  [ "$status" -eq 0 ]
  [ "$output" = "4" ]

  # Indexing parses RFC 4180 and identifies exactly 3 logical records (header + 2 rows)
  run_lab "$PROBE" indexed-index "$BATS_TEST_TMPDIR/multiline.csv" "$BATS_TEST_TMPDIR/multiline.idx"
  [ "$status" -eq 0 ]

  run_lab "$PROBE" indexed-count "$BATS_TEST_TMPDIR/multiline.idx"
  [ "$status" -eq 0 ]
  [ "$output" = "3" ]
}

@test "naive cut splits on delimiter inside quoted field" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/quoted_commas.csv"
id,name,city
1,"Doe, Jane",Boston
2,"Smith, John",Chicago
EOF

  # Naive cut splits field 2 at the embedded comma
  run_lab "$PROBE" naive-cut "$BATS_TEST_TMPDIR/quoted_commas.csv" 2
  [ "$status" -eq 0 ]
  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected_broken.txt"
name
"Doe
"Smith
EOF
  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/expected_broken.txt")" ]
}

@test "indexed-index creates binary (N+1)*8 byte index layout" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/simple.csv"
a,b
1,2
3,4
5,6
EOF

  run_lab "$PROBE" indexed-index "$BATS_TEST_TMPDIR/simple.csv" "$BATS_TEST_TMPDIR/simple.idx"
  [ "$status" -eq 0 ]

  # Total records: 4 (header + 3 data records). Expected index size = (4 + 1) * 8 = 40 bytes.
  idx_size=$(wc -c <"$BATS_TEST_TMPDIR/simple.idx" | tr -d ' ')
  [ "$idx_size" -eq 40 ]

  run_lab "$PROBE" validate-index "$BATS_TEST_TMPDIR/simple.csv" "$BATS_TEST_TMPDIR/simple.idx"
  [ "$status" -eq 0 ]
  [[ "$output" =~ "Valid index: 4 records, 40 bytes" ]]
}

@test "indexed-slice extracts exact record range using byte offsets" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/slice_data.csv"
id,title
1,"First
record"
2,"Second
record"
3,"Third
record"
4,"Fourth
record"
EOF

  run_lab "$PROBE" indexed-index "$BATS_TEST_TMPDIR/slice_data.csv" "$BATS_TEST_TMPDIR/slice_data.idx"
  [ "$status" -eq 0 ]

  # Slice records starting at index 2 (record 2) with length 2 (records 2 and 3)
  run_lab "$PROBE" indexed-slice "$BATS_TEST_TMPDIR/slice_data.csv" "$BATS_TEST_TMPDIR/slice_data.idx" 2 2
  [ "$status" -eq 0 ]

  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected_slice.txt"
2,"Second
record"
3,"Third
record"
EOF
  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/expected_slice.txt")" ]
}

@test "validate-index detects and rejects truncated index file" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/valid.csv"
col1,col2
10,20
30,40
EOF

  run_lab "$PROBE" indexed-index "$BATS_TEST_TMPDIR/valid.csv" "$BATS_TEST_TMPDIR/valid.idx"
  [ "$status" -eq 0 ]

  # Truncate index by 4 bytes (invalidating 8-byte alignment)
  orig_size=$(wc -c <"$BATS_TEST_TMPDIR/valid.idx" | tr -d ' ')
  trunc_size=$((orig_size - 4))
  dd if="$BATS_TEST_TMPDIR/valid.idx" of="$BATS_TEST_TMPDIR/corrupt.idx" bs=1 count="$trunc_size" 2>/dev/null

  run_lab "$PROBE" validate-index "$BATS_TEST_TMPDIR/valid.csv" "$BATS_TEST_TMPDIR/corrupt.idx"
  [ "$status" -ne 0 ]
  [[ "$output" =~ "is not a multiple of 8 bytes" ]]
}

@test "hash-join succeeds on unsorted data where line join fails" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/users.csv"
uid,username
30,charlie
10,alice
20,bob
EOF

  cat <<'EOF' >"$BATS_TEST_TMPDIR/orders.csv"
oid,uid,item
101,20,book
102,10,pen
103,30,laptop
EOF

  # Hash join matches records without pre-sorting
  run_lab "$PROBE" hash-join "$BATS_TEST_TMPDIR/users.csv" "$BATS_TEST_TMPDIR/orders.csv" 1 2
  [ "$status" -eq 0 ]

  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected_hash_join.csv"
uid,username,oid,uid,item
20,bob,101,20,book
10,alice,102,10,pen
30,charlie,103,30,laptop
EOF
  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/expected_hash_join.csv")" ]

  # Naive line join expects sorted order by key.
  # A merge join over these unsorted inputs can pair at most one data row (key 30), never all three.
  # Where join is absent (the Alpine image), the comparison would be vacuous, so skip it.
  command -v join >/dev/null 2>&1 || skip "join is not installed"
  run_lab "$PROBE" naive-join "$BATS_TEST_TMPDIR/users.csv" "$BATS_TEST_TMPDIR/orders.csv" 1 2
  naive_join_output="$output"
  naive_data_rows=$(printf '%s\n' "$naive_join_output" | grep -c '^[0-9]' || true)
  [ "$naive_data_rows" -lt 3 ]
}

@test "shell IFS comma splitting splits inside double quotes across all families" {
  # shellcheck disable=SC2016
  run_lab_c '
    line='\''1,"Alpha, Beta",300'\''
    IFS=, read -r f1 f2 f3 f4 <<EOF
$line
EOF
    printf "f1=%s|f2=%s|f3=%s|f4=%s\n" "$f1" "$f2" "$f3" "$f4"
  '
  [ "$status" -eq 0 ]

  # Every standard POSIX shell splits on byte value 0x2C irrespective of surrounding quotes
  case "$(lab_shell_family)" in
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$output" = 'f1=1|f2="Alpha|f3= Beta"|f4=300' ]
      ;;
    *)
      false
      ;;
  esac
}
