#!/usr/bin/env bats
# Tests for csv-tool.sh demonstrating RFC 4180 parsing vs line-oriented failure modes.

setup() {
  load ../lib/lab
  CSV_TOOL="$BATS_TEST_DIRNAME/csv-tool.sh"
}

@test "naive-cut splits on commas inside quoted strings, corrupting fields" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/input.csv"
"Smith, John",30,Engineer
"Doe, Jane",25,Manager
EOF

  run_lab "$CSV_TOOL" naive-cut "$BATS_TEST_TMPDIR/input.csv"
  [ "$status" -eq 0 ]
  # Naive cut splits "Smith, John" at the quoted comma and drops the real second field (30)
  [ "${#lines[@]}" -eq 2 ]
  [ "${lines[0]}" = '"Smith, John"' ]
  [ "${lines[1]}" = '"Doe, Jane"' ]
}

@test "naive-cut breaks a multi-line quoted record into phantom rows" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/ex.csv"
"Smith, John",30,Engineer
1,"first line
second line",done
"He said, ""Hello!""",accepted
EOF

  run_lab "$CSV_TOOL" naive-cut "$BATS_TEST_TMPDIR/ex.csv"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 4 ]
  [ "${lines[0]}" = '"Smith, John"' ]
  [ "${lines[1]}" = '1,"first line' ]
  [ "${lines[2]}" = 'second line",done' ]
  [ "${lines[3]}" = '"He said, ""Hello!"""' ]
}

@test "naive-read leaves carriage return attached on CRLF inputs" {
  # Generate CRLF CSV
  printf 'id,status\r\n1,active\r\n' >"$BATS_TEST_TMPDIR/input_crlf.csv"

  run_lab "$CSV_TOOL" naive-read "$BATS_TEST_TMPDIR/input_crlf.csv"
  [ "$status" -eq 0 ]
  # In line-oriented read, status retains trailing \r
  expected=$(printf 'col1=[id] col2=[status\r] rem=[]')
  [ "${lines[0]}" = "$expected" ]
}

@test "parse-fsm correctly handles embedded commas within quotes" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/input.csv"
"Smith, John",30,Engineer
"Doe, Jane",25,Manager
EOF

  run_lab "$CSV_TOOL" parse-fsm "$BATS_TEST_TMPDIR/input.csv"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "3 fields: <Smith, John> <30> <Engineer>" ]
  [ "${lines[1]}" = "3 fields: <Doe, Jane> <25> <Manager>" ]
}

@test "parse-fsm correctly handles multi-line records with embedded newlines" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/multiline.csv"
1,"first line
second line",done
2,simple,ready
EOF

  run_lab "$CSV_TOOL" parse-fsm "$BATS_TEST_TMPDIR/multiline.csv"
  [ "$status" -eq 0 ]
  expected=$(printf '3 fields: <1> <first line\nsecond line> <done>\n3 fields: <2> <simple> <ready>')
  [ "$output" = "$expected" ]
}

@test "parse-fsm correctly parses doubled double quotes as an escaped quote" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/quotes.csv"
"He said, ""Hello!""",accepted
EOF

  run_lab "$CSV_TOOL" parse-fsm "$BATS_TEST_TMPDIR/quotes.csv"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '2 fields: <He said, "Hello!"> <accepted>' ]
}

@test "parse-fsm detects unclosed quote at EOF and exits non-zero" {
  printf '"unclosed field,val' >"$BATS_TEST_TMPDIR/unclosed.csv"

  run_lab "$CSV_TOOL" parse-fsm "$BATS_TEST_TMPDIR/unclosed.csv"
  [ "$status" -eq 2 ]
}

@test "strip-bom removes UTF-8 BOM from the first field" {
  # Build 3-byte UTF-8 BOM (0xEF 0xBB 0xBF) using octal escapes in printf
  printf '\357\273\277id,name\n1,alice\n' >"$BATS_TEST_TMPDIR/bom.csv"

  run_lab "$CSV_TOOL" strip-bom "$BATS_TEST_TMPDIR/bom.csv"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "id,name" ]
  [ "${lines[1]}" = "1,alice" ]
}

@test "sanitize neutralizes formula injection triggers by prepending single quote" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/injection.csv"
=cmd|'/C calc'!A0,+1234,-5678,@sum(A1:A2),normal
EOF

  run_lab "$CSV_TOOL" sanitize "$BATS_TEST_TMPDIR/injection.csv"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "\"'=cmd|'/C calc'!A0\",\"'+1234\",\"'-5678\",\"'@sum(A1:A2)\",\"normal\"" ]
}
