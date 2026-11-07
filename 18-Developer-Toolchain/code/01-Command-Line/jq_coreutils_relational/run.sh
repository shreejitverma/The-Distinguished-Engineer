#!/usr/bin/env bash
set -euo pipefail

# Tool preconditions the harness has no tag for: jq >= 1.7 (literal-preserving numbers) and
# Mike Farah's yq v4 (the -o=json -I=0 flags). Fail loudly rather than record a different output.
jq_ver="$(jq --version 2>/dev/null | sed 's/^jq-//')"
case "${jq_ver}" in
  1.[7-9]*|1.[1-9][0-9]*|[2-9].*) ;;
  *) echo "precondition failed: jq >= 1.7 required, found '${jq_ver:-none}'" >&2; exit 2 ;;
esac
case "$(yq --version 2>/dev/null)" in
  *mikefarah*" v4."*) ;;
  *) echo "precondition failed: mikefarah yq v4 required" >&2; exit 2 ;;
esac

# Deterministic environment
export LC_ALL=C
export LANG=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

cd "${WORK_DIR}"

# 1. JQ in-memory relational hash join and aggregation
cat << 'JSON' > departments.json
[
  {"dept_id": "D10", "dept_name": "Engineering"},
  {"dept_id": "D20", "dept_name": "Product"},
  {"dept_id": "D30", "dept_name": "Security"}
]
JSON

cat << 'JSON' > employees.json
[
  {"emp_id": "E101", "name": "Alice", "dept_id": "D10", "salary": 180000},
  {"emp_id": "E102", "name": "Bob", "dept_id": "D10", "salary": 165000},
  {"emp_id": "E103", "name": "Carol", "dept_id": "D20", "salary": 170000},
  {"emp_id": "E104", "name": "Dan", "dept_id": "D40", "salary": 150000}
]
JSON

echo "=== 1. JQ In-Memory Hash Join ==="
jq -n --slurpfile depts departments.json --slurpfile emps employees.json '
  (INDEX($depts[0][]; .dept_id)) as $dmap |
  $emps[0][] |
  {
    emp_id: .emp_id,
    name: .name,
    dept: ($dmap[.dept_id].dept_name // "Unassigned"),
    salary: .salary
  }
' -c

echo ""
echo "=== 2. JQ Stream Truncation for Bounded Memory ==="
cat << 'JSON' > nested_stream.json
{
  "status": "success",
  "audit": {
    "records": [
      {"event_id": "EV-01", "action": "LOGIN", "status": "ALLOW"},
      {"event_id": "EV-02", "action": "WRITE", "status": "DENY"},
      {"event_id": "EV-03", "action": "LOGOUT", "status": "ALLOW"}
    ]
  }
}
JSON

# Stream records under ["audit", "records", index] without loading root AST
jq -n -c --stream '
  fromstream(3 | truncate_stream(inputs))
' nested_stream.json

# Incremental proof: on a document that never closes, --stream has already emitted the completed
# leaves before the parse error, while whole-document mode emits nothing. Exit 5 is jq's error status.
echo "--- Truncated input: --stream emits before the error, DOM mode emits nothing ---"
trunc_rc=0
printf '{"a":[1,2,3' | jq -c --stream . 2>/dev/null || trunc_rc=$?
echo "stream exit=${trunc_rc}"
dom_rc=0
printf '{"a":[1,2,3' | jq -c . 2>/dev/null || dom_rc=$?
echo "dom exit=${dom_rc}"

echo ""
echo "=== 3. Coreutils Relational Joins (LC_ALL=C) ==="
# Relational files: employee master and payroll
cat << 'CSV' | sort -t ',' -k1,1 > emp_master.csv
E101,Alice,Engineer
E102,Bob,Manager
E103,Carol,Designer
E105,Eve,Architect
CSV

cat << 'CSV' | sort -t ',' -k1,1 > payroll.csv
E101,180000,US
E102,165000,US
E104,150000,EU
E105,210000,US
CSV

echo "--- Inner Join ---"
join -t ',' -1 1 -2 1 emp_master.csv payroll.csv

echo "--- Left Outer Join (Unmatched Replaced by NULL) ---"
join -t ',' -a 1 -1 1 -2 1 -e 'NULL' -o 1.1,1.2,1.3,2.2,2.3 emp_master.csv payroll.csv

echo "--- Full Outer Join ---"
join -t ',' -a 1 -a 2 -1 1 -2 1 -e 'NULL' -o 0,1.2,1.3,2.2,2.3 emp_master.csv payroll.csv

echo "--- Anti-Join (In Master but No Payroll) ---"
join -t ',' -v 1 -1 1 -2 1 emp_master.csv payroll.csv

echo ""
echo "=== 4. Set Operations with comm ==="
cut -d ',' -f 1 emp_master.csv > emp_ids.txt
cut -d ',' -f 1 payroll.csv > payroll_ids.txt

echo "--- Common IDs (Intersection) ---"
comm -12 emp_ids.txt payroll_ids.txt

echo "--- Master Only (Difference) ---"
comm -23 emp_ids.txt payroll_ids.txt

echo "--- Payroll Only (Difference) ---"
comm -13 emp_ids.txt payroll_ids.txt

echo ""
echo "=== 5. Hybrid Pipeline: JSON Stream -> TSV Coreutils -> JSON Output ==="
cat << 'NDJSON' > raw_users.jsonl
{"id":"U1","name":"Alice","tier":"gold"}
{"id":"U2","name":"Bob","tier":"silver"}
{"id":"U3","name":"Charlie","tier":"bronze"}
NDJSON

cat << 'NDJSON' > raw_orders.jsonl
{"order_id":"1001","user_id":"U1","amount":500}
{"order_id":"1002","user_id":"U1","amount":250}
{"order_id":"1003","user_id":"U2","amount":120}
{"order_id":"1004","user_id":"U4","amount":80}
NDJSON

# Step 1: Project JSON to sorted TSV
jq -r '[.id, .name, .tier] | @tsv' raw_users.jsonl | sort -t $'\t' -k1,1 > sorted_users.tsv
jq -r '[.user_id, .order_id, .amount] | @tsv' raw_orders.jsonl | sort -t $'\t' -k1,1 > sorted_orders.tsv

# Step 2: Join streams using coreutils join (1-to-many join)
# Format: user_id, order_id, amount, name, tier
join -t $'\t' -1 1 -2 1 -o 1.1,2.2,2.3,1.2,1.3 sorted_users.tsv sorted_orders.tsv > joined_stream.tsv

# Step 3: Stream back into JSON objects
jq -R -c 'split("\t") | {user_id: .[0], order_id: .[1], amount: (.[2] | tonumber), name: .[3], tier: .[4]}' joined_stream.tsv

echo ""
echo "=== 6. Failure Modes and Hazards ==="
echo "--- Unsorted Input Detection with sort -c ---"
check_rc=0
printf "E102,Bob\nE101,Alice\n" | sort -c -t ',' -k1,1 2>&1 || check_rc=$?
echo "sort -c exit=${check_rc}"

echo "--- IEEE 754 Precision Truncation in JQ ---"
# 2^53 + 1 = 9007199254740993 cannot be represented accurately in 64-bit float
echo '{"raw_id": 9007199254740993}' | jq -c '{input: .raw_id, evaluated: (.raw_id + 0), equality_collapsed: ((.raw_id + 0) == 9007199254740993)}'

echo "--- YAML 1.2 String Preservation in yq ---"
echo 'country: NO' | yq -o=json -I=0 '.'

echo ""
echo "=== 7. Collation Mismatch: the Headline Join Failure ==="
# Keys whose order differs between byte collation and the en_US.UTF-8 locale.
printf 'ab-c,1\nab_d,2\nabb,3\n' > left.csv
printf 'ab-c,x\nab_d,y\nabb,z\n' > right.csv
LOCALE_NAME="$(locale -a | grep -ix 'en_US\.utf-\?8' | head -1 || true)"
if [ -z "${LOCALE_NAME}" ]; then
  echo "skipped: no en_US.UTF-8 locale installed"
else
  LC_ALL="${LOCALE_NAME}" sort -t ',' -k1,1 left.csv > left_locale.csv
  LC_ALL=C sort -t ',' -k1,1 right.csv > right_c.csv
  echo "--- Left sorted under en_US.UTF-8, right under C, join under C ---"
  echo "left key order:  $(cut -d ',' -f 1 left_locale.csv | paste -sd ' ' -)"
  echo "right key order: $(cut -d ',' -f 1 right_c.csv | paste -sd ' ' -)"
  mismatch_rc=0
  mismatch_rows="$(join -t ',' left_locale.csv right_c.csv 2>/dev/null)" || mismatch_rc=$?
  echo "rows joined: $(printf '%s\n' "${mismatch_rows}" | grep -c .) of 3 (exit=${mismatch_rc})"
  check_rc=0
  sort -c -t ',' -k1,1 left_locale.csv 2>/dev/null || check_rc=$?
  echo "sort -c on the locale-sorted file under C: exit=${check_rc}"
  echo "--- Both sorted under C ---"
  LC_ALL=C sort -t ',' -k1,1 left.csv > left_c.csv
  fixed_rc=0
  fixed_rows="$(join -t ',' left_c.csv right_c.csv)" || fixed_rc=$?
  echo "rows joined: $(printf '%s\n' "${fixed_rows}" | grep -c .) of 3 (exit=${fixed_rc})"
fi
