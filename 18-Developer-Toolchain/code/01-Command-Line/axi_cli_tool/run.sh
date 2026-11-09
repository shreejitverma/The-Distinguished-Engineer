#!/usr/bin/env bash
# Requirements: none gated; python3 and bash on the recording platform (the harness's empty form is no requires line).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

DB_PATH="${TMP_DIR}/tasks.json"
TOOL="${SCRIPT_DIR}/axi_task.py"
export AXI_TOOL="${TOOL}" AXI_DB="${DB_PATH}" AXI_TMP="${TMP_DIR}"

echo "=== 1. Content-First Default (Principle 8) ==="
# Running with no arguments outputs active tasks, not help text
python3 "${TOOL}" --db="${DB_PATH}"
python3 "${TOOL}" --db="${DB_PATH}" > "${TMP_DIR}/noargs.txt"
python3 "${TOOL}" --db="${DB_PATH}" list > "${TMP_DIR}/list.txt"
if cmp -s "${TMP_DIR}/noargs.txt" "${TMP_DIR}/list.txt"; then echo "no-args output equals 'list': True"; else echo "no-args output equals 'list': False"; fi
if grep -q "Usage:" "${TMP_DIR}/noargs.txt"; then echo "no-args output contains help text: True"; else echo "no-args output contains help text: False"; fi

echo ""
echo "=== 2. Minimal Default Schema & Aggregates (Principles 1, 2, 4) ==="
# TOON format: schema declaration and aggregated summary header
python3 "${TOOL}" --db="${DB_PATH}" --format=toon | head -n 2
python3 - <<'PY'
import os, subprocess
out = subprocess.check_output(["python3", os.environ["AXI_TOOL"], "--db=" + os.environ["AXI_DB"], "--format=toon"], text=True)
rows = [l for l in out.splitlines() if l.startswith("  ")]
print(f"row count matches tasks[N]: {out.splitlines()[1].startswith('tasks[' + str(len(rows)) + ']')}")
print(f"fields per list row: {len(rows[0].split(','))}")
print(f"description in list output: {'description' in out}")
PY

echo ""
echo "=== 3. Content Truncation & --full Escape Hatch (Principle 3) ==="
echo "--- Default Truncated View ---"
python3 "${TOOL}" --db="${DB_PATH}" show 1
echo "--- Full Untruncated View ---"
python3 "${TOOL}" --db="${DB_PATH}" show 1 --full

echo ""
echo "=== 4. Definitive Empty State (Principle 5) ==="
# Explicit 0-result confirmation rather than silent empty output
python3 "${TOOL}" --db="${DB_PATH}" --status=closed --format=toon
# Delete task 3 to make closed filter return 0 results
python3 "${TOOL}" --db="${DB_PATH}" delete 3 >/dev/null
python3 "${TOOL}" --db="${DB_PATH}" --status=closed --format=toon

echo ""
echo "=== 5. Structured Errors and Standard Exit Codes (Principle 6) ==="
echo "--- Missing Required Flag (EX_USAGE = 64) ---"
set +e
python3 "${TOOL}" --db="${DB_PATH}" create 2> "${TMP_DIR}/err_usage.json"
CODE_USAGE=$?
set -e
cat "${TMP_DIR}/err_usage.json"
echo "exit_status: ${CODE_USAGE}"

echo "--- Unknown Flag Rejected By Name (EX_USAGE = 64) ---"
set +e
python3 "${TOOL}" --db="${DB_PATH}" --bogus 2> "${TMP_DIR}/err_flag.json"
CODE_FLAG=$?
set -e
cat "${TMP_DIR}/err_flag.json"
echo "exit_status: ${CODE_FLAG}"

echo "--- Non-Existent Resource (EX_NOINPUT = 66) ---"
set +e
python3 "${TOOL}" --db="${DB_PATH}" show 999 2> "${TMP_DIR}/err_notfound.json"
CODE_NOTFOUND=$?
set -e
cat "${TMP_DIR}/err_notfound.json"
echo "exit_status: ${CODE_NOTFOUND}"

echo "--- Idempotent Deletion (Success = 0) ---"
python3 "${TOOL}" --db="${DB_PATH}" delete 999 --ignore-missing
echo "exit_status: $?"

echo ""
echo "=== 6. Contextual Disclosure & Stream Separation (Principle 9 & Streams) ==="
# Creation prints resource ID on stdout and next steps on stderr
python3 "${TOOL}" --db="${DB_PATH}" create --title="Ship v1.0 release" > "${TMP_DIR}/out.txt" 2> "${TMP_DIR}/err.txt"
echo "stdout (data):"
cat "${TMP_DIR}/out.txt"
echo "stderr (diagnostics & guidance):"
cat "${TMP_DIR}/err.txt"

echo ""
echo "=== 7. Color Precedence (NO_COLOR vs FORCE_COLOR) ==="
# Each case runs with a minimal environment so only the variables under test are set.
python3 - <<'PY'
import os, subprocess
tool, db = os.environ["AXI_TOOL"], os.environ["AXI_DB"]

def has_ansi(env_extra, *args):
    env = {"PATH": os.environ["PATH"], **env_extra}
    out = subprocess.check_output(["python3", tool, "--db=" + db, "--format=human", *args], env=env, text=True)
    return "\033[" in out

print(f"piped, no variables: ANSI escapes: {has_ansi({})}")
print(f"FORCE_COLOR=1: ANSI escapes: {has_ansi({'FORCE_COLOR': '1'})}")
print(f"NO_COLOR=1 alone: ANSI escapes: {has_ansi({'NO_COLOR': '1'})}")
print(f"FORCE_COLOR=1 and NO_COLOR=1 (this lab: NO_COLOR wins): ANSI escapes: {has_ansi({'FORCE_COLOR': '1', 'NO_COLOR': '1'})}")
print(f"NO_COLOR=1 and --color=always (flag wins): ANSI escapes: {has_ansi({'NO_COLOR': '1'}, '--color=always')}")
PY

echo ""
echo "=== 8. Payload Size: Projection vs Format (Principles 1 and 2) ==="
python3 - <<'PY'
import json, os, subprocess
tool, db = os.environ["AXI_TOOL"], os.environ["AXI_DB"]

def run(fmt):
    return subprocess.check_output(["python3", tool, "--db=" + db, "--format=" + fmt], text=True)

full = json.loads(run("json"))
toon = run("toon")
fields = ("id", "title", "status", "priority")
projected = {"summary": full["summary"], "tasks": [{k: t[k] for k in fields} for t in full["tasks"]]}
size = lambda text: len(text.encode("utf-8"))
json_full = size(json.dumps(full, indent=2) + "\n")
json_proj = size(json.dumps(projected, indent=2) + "\n")
json_compact = size(json.dumps(projected, separators=(",", ":")) + "\n")
toon_size = size(toon)
pct = lambda a, b: f"{(a - b) / a * 100:.1f}%"
print(f"JSON, all fields, indented: {json_full} bytes")
print(f"JSON, 4 fields, indented: {json_proj} bytes")
print(f"JSON, 4 fields, compact: {json_compact} bytes")
print(f"TOON, 4 fields: {toon_size} bytes")
print(f"Projection alone (all fields -> 4 fields, indented JSON): {pct(json_full, json_proj)}")
print(f"Format alone (4-field indented JSON -> TOON): {pct(json_proj, toon_size)}")
print(f"Format alone (4-field compact JSON -> TOON): {pct(json_compact, toon_size)}")
PY

echo ""
echo "=== 9. SIGPIPE Pipeline Resilience ==="
# The list must exceed the pipe buffer (64 KiB on Linux), or the tool exits before head closes the pipe.
python3 - <<'PY'
import json, os, subprocess, sys

tool, tmp = os.environ["AXI_TOOL"], os.environ["AXI_TMP"]
big_db = os.path.join(tmp, "big.json")
with open(big_db, "w", encoding="utf-8") as f:
    json.dump([{"id": i, "title": f"Task number {i}", "status": "open", "priority": "low", "description": ""}
               for i in range(1, 6001)], f)
cmd = ["python3", tool, "--db=" + big_db, "--format=toon"]
total = len(subprocess.check_output(cmd))
print(f"list output exceeds 64 KiB: {total > 65536}")

def through_head(argv):
    p1 = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(["head", "-n", "1"], stdin=p1.stdout, stdout=subprocess.PIPE)
    p1.stdout.close()
    head_out, _ = p2.communicate()
    _, err = p1.communicate()
    return head_out.decode().strip(), p1.returncode, err

head_out, rc, err = through_head(cmd)
print(f"pipeline head received: {head_out}")
print(f"tool killed by SIGPIPE (returncode == -13): {rc == -13}")
print(f"no BrokenPipeError on stderr: {b'BrokenPipeError' not in err}")

# Control: neutralize the tool's SIG_DFL line; CPython's startup SIG_IGN then turns the failed write into an exception.
neutered = ["python3", "-c",
            "import runpy, signal, sys; signal.signal = lambda *a, **k: None; "
            "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')",
            tool, "--db=" + big_db, "--format=toon"]
_, rc, err = through_head(neutered)
print(f"control without SIG_DFL exits 1 or 120: {rc in (1, 120)}")
print(f"control without SIG_DFL prints BrokenPipeError: {b'BrokenPipeError' in err}")
PY

echo ""
echo "=== 10. TOON Quoting of Delimiters, Colons, and Quotes ==="
python3 "${TOOL}" --db="${DB_PATH}" create --title='Fix, then ship: "v2"' >/dev/null 2>&1
python3 "${TOOL}" --db="${DB_PATH}" --format=toon | grep 'then ship'

echo ""
echo "=== 11. Help Is Explicit and Concise (Principle 10) ==="
set +e
python3 "${TOOL}" --help > "${TMP_DIR}/help.txt" 2> "${TMP_DIR}/help.err"
CODE_HELP=$?
set -e
echo "exit_status: ${CODE_HELP}"
echo "help on stdout: $(if [ -s "${TMP_DIR}/help.txt" ]; then echo True; else echo False; fi)"
echo "help stderr empty: $(if [ -s "${TMP_DIR}/help.err" ]; then echo False; else echo True; fi)"
echo "help lines: $(wc -l < "${TMP_DIR}/help.txt" | tr -d ' ')"
echo "help has examples: $(if grep -q '^Examples:' "${TMP_DIR}/help.txt"; then echo True; else echo False; fi)"
