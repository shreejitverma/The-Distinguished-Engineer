#!/bin/sh
# process_logs.sh: aggregate and analyze HTTP access logs joined with service metadata.
# Uses Miller to perform in-process joins, filtering, and per-group latency statistics.
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
MLR=${MLR:-$(command -v mlr 2>/dev/null || echo "$SCRIPT_DIR/../../bin/mlr")}

if [ ! -x "$MLR" ]; then
  printf 'process_logs.sh: mlr binary not found or not executable: %s\n' "$MLR" >&2
  exit 1
fi

usage() {
  printf 'Usage: %s <access_logs.csv> <services.csv>\n' "$0" >&2
  exit 2
}

[ "$#" -eq 2 ] || usage

LOGS_FILE=$1
SERVICES_FILE=$2

[ -f "$LOGS_FILE" ] || {
  printf 'File not found: %s\n' "$LOGS_FILE" >&2
  exit 1
}
[ -f "$SERVICES_FILE" ] || {
  printf 'File not found: %s\n' "$SERVICES_FILE" >&2
  exit 1
}

# In-process pipeline:
# 1. drop health-check requests
# 2. join with services metadata on "path" (unsorted hash join)
# 3. classify each request's severity in the DSL
# 4. aggregate count, min, mean, and max latency grouped by service_tier, severity
# 5. sort the groups by service_tier, then severity
# shellcheck disable=SC2016 # single quotes protect Miller DSL expressions from shell expansion
"$MLR" --norc --csv \
  filter '$path != "/healthz"' \
  'then' join -u -j path -f "$SERVICES_FILE" \
  'then' put '
    if ($status >= 500) {
      $severity = "CRITICAL"
    } elif ($latency_ms > 200) {
      $severity = "WARNING"
    } else {
      $severity = "NORMAL"
    }
  ' \
  'then' stats1 -a count,min,mean,max -f latency_ms -g service_tier,severity \
  'then' sort -f service_tier,severity \
  "$LOGS_FILE"
