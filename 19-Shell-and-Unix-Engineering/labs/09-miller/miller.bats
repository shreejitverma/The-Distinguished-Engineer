#!/usr/bin/env bats
# Tests for Miller (mlr) record-stream processing, DSL evaluation, and CSV handling.

setup() {
  load ../lib/lab
  LAB_DIR="$BATS_TEST_DIRNAME"
  MLR=${MLR:-$(command -v mlr 2>/dev/null || echo "$BATS_TEST_DIRNAME/../../bin/mlr")}
  if [ ! -x "$MLR" ]; then
    printf 'mlr binary not found or not executable: %s\n' "$MLR" >&2
    return 1
  fi
  export MLR
  SCRIPT="$LAB_DIR/process_logs.sh"
}

@test "process_logs.sh aggregates metrics correctly across joined service metadata" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/services.csv"
path,service_tier,team
/api/v1/checkout,tier-1,payments
/api/v1/catalog,tier-2,search
/api/v1/login,tier-1,auth
EOF

  cat <<'EOF' >"$BATS_TEST_TMPDIR/logs.csv"
id,path,status,latency_ms
1,/api/v1/checkout,200,50
2,/api/v1/checkout,500,250
3,/api/v1/catalog,200,80
4,/api/v1/catalog,200,220
5,/healthz,200,5
6,/api/v1/login,200,40
EOF

  run_lab "$SCRIPT" "$BATS_TEST_TMPDIR/logs.csv" "$BATS_TEST_TMPDIR/services.csv"
  [ "$status" -eq 0 ]

  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected.csv"
service_tier,severity,latency_ms_count,latency_ms_min,latency_ms_mean,latency_ms_max
tier-1,CRITICAL,1,250,250,250
tier-1,NORMAL,2,40,45,50
tier-2,NORMAL,1,80,80,80
tier-2,WARNING,1,220,220,220
EOF

  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/expected.csv")" ]
}

@test "heterogeneous CSV records cause schema change error without unsparsify" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/hetero.dkvp"
a=1,b=2
a=3,c=4
a=5,b=6
EOF

  # shellcheck disable=SC2016
  run_lab_c '"$MLR" --norc --dkvp --ocsv cat "$1" 2>&1' sh "$BATS_TEST_TMPDIR/hetero.dkvp"
  [ "$status" -eq 1 ]
  [[ "$output" =~ "CSV schema change" ]]
}

@test "unsparsify rectifies heterogeneous CSV records into rectangular output" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/hetero.dkvp"
a=1,b=2
a=3,c=4
a=5,b=6
EOF

  # shellcheck disable=SC2016
  run_lab_c '"$MLR" --norc --dkvp --ocsv unsparsify "$1"' sh "$BATS_TEST_TMPDIR/hetero.dkvp"
  [ "$status" -eq 0 ]

  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected_unsparsify.csv"
a,b,c
1,2,
3,,4
5,6,
EOF

  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/expected_unsparsify.csv")" ]
}

@test "single-process then-chaining matches multi-process shell pipeline output" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/data.csv"
id,category,val
1,alpha,10
2,beta,20
3,alpha,30
4,gamma,40
5,beta,50
EOF

  # Run fused in-process pipeline
  # shellcheck disable=SC2016
  run_lab_c '"$MLR" --norc --csv filter '"'"'$category != "gamma"'"'"' then put '"'"'$doubled = $val * 2'"'"' then stats1 -a sum -f doubled -g category then sort -f category "$1"' sh "$BATS_TEST_TMPDIR/data.csv"
  [ "$status" -eq 0 ]
  fused_out="$output"

  # Run multi-process pipe
  # shellcheck disable=SC2016
  run_lab_c '"$MLR" --norc --csv filter '"'"'$category != "gamma"'"'"' "$1" | "$MLR" --norc --csv put '"'"'$doubled = $val * 2'"'"' | "$MLR" --norc --csv stats1 -a sum -f doubled -g category | "$MLR" --norc --csv sort -f category' sh "$BATS_TEST_TMPDIR/data.csv"
  [ "$status" -eq 0 ]
  pipe_out="$output"

  [ "$fused_out" = "$pipe_out" ]

  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected_then.csv"
category,doubled_sum
alpha,80
beta,140
EOF

  [ "$fused_out" = "$(cat "$BATS_TEST_TMPDIR/expected_then.csv")" ]
}

@test "absent-coalesce ?? vs absent-or-empty ??? distinguish undefined from empty fields" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/coalesce.csv"
name,opt
alice,present
bob,
carol,present
EOF

  # shellcheck disable=SC2016
  run_lab_c '"$MLR" --norc --csv put '"'"'
    $from_absent = $missing ?? "ABSENT_FALLBACK";
    $null_coal   = $opt ?? "NULL_FALLBACK";
    $empty_coal  = $opt ??? "EMPTY_FALLBACK";
  '"'"' "$1"' sh "$BATS_TEST_TMPDIR/coalesce.csv"
  [ "$status" -eq 0 ]

  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected_coalesce.csv"
name,opt,from_absent,null_coal,empty_coal
alice,present,ABSENT_FALLBACK,present,present
bob,,ABSENT_FALLBACK,,EMPTY_FALLBACK
carol,present,ABSENT_FALLBACK,present,present
EOF

  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/expected_coalesce.csv")" ]
}

@test "mlr join anti-join emits only unlinked records from the left file" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/left.csv"
id,name
1,alice
2,bob
3,carol
4,dave
EOF

  cat <<'EOF' >"$BATS_TEST_TMPDIR/right.csv"
id,role
2,admin
4,guest
EOF

  # Left anti-join: emit records in left but not in right
  # shellcheck disable=SC2016
  run_lab_c '"$MLR" --norc --csv join -u -j id --np --ul -f "$1" "$2"' sh "$BATS_TEST_TMPDIR/left.csv" "$BATS_TEST_TMPDIR/right.csv"
  [ "$status" -eq 0 ]

  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected_antijoin.csv"
id,name
1,alice
3,carol
EOF

  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/expected_antijoin.csv")" ]
}

@test "wide-to-long and long-to-wide reshape pivots preserve data integrity" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/wide.csv"
year,q1,q2
2025,100,200
2026,150,250
EOF

  # Reshape wide to long
  # shellcheck disable=SC2016
  run_lab_c '"$MLR" --norc --csv reshape -i q1,q2 -o quarter,revenue "$1"' sh "$BATS_TEST_TMPDIR/wide.csv"
  [ "$status" -eq 0 ]

  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected_long.csv"
year,quarter,revenue
2025,q1,100
2025,q2,200
2026,q1,150
2026,q2,250
EOF

  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/expected_long.csv")" ]

  # Reshape back long to wide
  echo "$output" >"$BATS_TEST_TMPDIR/actual_long.csv"
  # shellcheck disable=SC2016
  run_lab_c '"$MLR" --norc --csv reshape -s quarter,revenue "$1"' sh "$BATS_TEST_TMPDIR/actual_long.csv"
  [ "$status" -eq 0 ]

  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/wide.csv")" ]
}

@test "DSL state accumulation across records using out-of-stream variables" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/stream.csv"
item,qty,price
widget,5,10.0
gadget,2,25.0
widget,3,10.0
EOF

  # shellcheck disable=SC2016
  run_lab_c '"$MLR" --norc --csv put -q '"'"'
    $cost = $qty * $price;
    @total_cost += $cost;
    @count += 1;
    @by_item[$item] += $cost;
    end {
      emit (@total_cost, @count)
    }
  '"'"' "$1"' sh "$BATS_TEST_TMPDIR/stream.csv"
  [ "$status" -eq 0 ]

  cat <<'EOF' >"$BATS_TEST_TMPDIR/expected_accum.csv"
total_cost,count
130,3
EOF

  [ "$output" = "$(cat "$BATS_TEST_TMPDIR/expected_accum.csv")" ]
}

@test "early pipeline termination triggers SIGPIPE in upstream producer with status 141" {
  # `set` is a special built-in: a failing `set -o pipefail` exits a POSIX shell
  # such as dash (status 2) even with `|| true`, so probe it in a subshell first.
  # shellcheck disable=SC2016
  run_lab_c '
    (set -o pipefail) 2>/dev/null && set -o pipefail
    "$MLR" --norc --csv seqgen --stop 1000000 | "$MLR" --norc --csv head -n 5 >/dev/null
  '
  case $(lab_shell_family) in
    dash | posh)
      # no pipefail: the pipeline status is the status of the last stage, head (0)
      [ "$status" -eq 0 ]
      ;;
    yash)
      # yash pipefail on SIGPIPE reports 397 (384 + 13)
      [ "$status" -eq 397 ]
      ;;
    ksh93)
      # macOS ksh93u+ 2012 reports 13; ksh93u+m pipes through sockets; assert only
      # that pipefail surfaced the upstream failure
      [ "$status" -ne 0 ]
      ;;
    *)
      # bash, zsh, busybox, mksh with pipefail report 141 (128 + 13)
      [ "$status" -eq 141 ]
      ;;
  esac
}
