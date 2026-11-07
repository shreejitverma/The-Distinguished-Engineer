#!/usr/bin/env bash
# requires: macos
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

CC="${CC:-cc}"

# Step 1: Codesigning requirement on Apple Silicon / macOS
"${CC}" "${SCRIPT_DIR}/minimal.c" -o "${WORK_DIR}/bin_unsigned"
codesign --remove-signature "${WORK_DIR}/bin_unsigned" 2>/dev/null || true

# Running an unsigned binary on arm64 macOS triggers kernel execution refusal (SIGKILL)
set +e
python3 -c "import subprocess, sys; res = subprocess.run([sys.argv[1]], capture_output=True); sys.exit(abs(res.returncode))" "${WORK_DIR}/bin_unsigned"
STATUS=$?
set -e

if [ "${STATUS}" -eq 9 ] || [ "${STATUS}" -eq 137 ]; then
    echo "UNSIGNED_EXEC: status=KILLED_SIGKILL"
else
    echo "UNSIGNED_EXEC: status=EXIT_${STATUS}"
fi

codesign -s - "${WORK_DIR}/bin_unsigned" 2>/dev/null
OUT=$("${WORK_DIR}/bin_unsigned")
if [ "${OUT}" = "MAIN_OK" ]; then
    echo "SIGNED_EXEC: status=SUCCESS"
else
    echo "SIGNED_EXEC: status=FAIL"
fi

# Step 2: Mach task port & Entitlements isolation (task_for_pid)
"${CC}" "${SCRIPT_DIR}/target.c" -o "${WORK_DIR}/target_unentitled"
"${CC}" "${SCRIPT_DIR}/target.c" -o "${WORK_DIR}/target_entitled"
codesign -s - -f --entitlements "${SCRIPT_DIR}/entitlements_task.plist" "${WORK_DIR}/target_entitled" 2>/dev/null

"${CC}" "${SCRIPT_DIR}/tracer.c" -o "${WORK_DIR}/tracer"

# Spawn un-entitled target in background
"${WORK_DIR}/target_unentitled" > "${WORK_DIR}/target_unentitled.out" 2>&1 &
TARGET_UN_PID=$!
while [ ! -s "${WORK_DIR}/target_unentitled.out" ]; do sleep 0.01; done
ADDR_UN=$(awk '{print $2}' "${WORK_DIR}/target_unentitled.out")

OUT_UN=$("${WORK_DIR}/tracer" "${TARGET_UN_PID}" "${ADDR_UN}")
echo "TASK_FOR_PID_UNENTITLED: $(echo "${OUT_UN}" | sed 's/TASK_FOR_PID: //')"
kill -9 "${TARGET_UN_PID}" 2>/dev/null || true
wait "${TARGET_UN_PID}" 2>/dev/null || true

# Spawn entitled target in background
"${WORK_DIR}/target_entitled" > "${WORK_DIR}/target_entitled.out" 2>&1 &
TARGET_EN_PID=$!
while [ ! -s "${WORK_DIR}/target_entitled.out" ]; do sleep 0.01; done
ADDR_EN=$(awk '{print $2}' "${WORK_DIR}/target_entitled.out")

OUT_EN=$("${WORK_DIR}/tracer" "${TARGET_EN_PID}" "${ADDR_EN}")
echo "TASK_FOR_PID_ENTITLED: $(echo "${OUT_EN}" | grep TASK_FOR_PID | sed 's/TASK_FOR_PID: //')"
echo "${OUT_EN}" | grep "VM_READ:"
kill -9 "${TARGET_EN_PID}" 2>/dev/null || true
wait "${TARGET_EN_PID}" 2>/dev/null || true

# Step 3: Hardened Runtime and DYLD_INSERT_LIBRARIES sanitization
"${CC}" -dynamiclib "${SCRIPT_DIR}/hook.c" -o "${WORK_DIR}/libhook.dylib"
codesign -s - "${WORK_DIR}/libhook.dylib" 2>/dev/null

"${CC}" "${SCRIPT_DIR}/app.c" -o "${WORK_DIR}/app_adhoc"

"${CC}" "${SCRIPT_DIR}/app.c" -o "${WORK_DIR}/app_hardened"
codesign -s - -f --options runtime "${WORK_DIR}/app_hardened" 2>/dev/null

"${CC}" "${SCRIPT_DIR}/app.c" -o "${WORK_DIR}/app_hardened_entitled"
codesign -s - -f --options runtime --entitlements "${SCRIPT_DIR}/entitlements_dyld.plist" "${WORK_DIR}/app_hardened_entitled" 2>/dev/null

# Interpose on standard ad-hoc binary
OUT1=$(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libhook.dylib" "${WORK_DIR}/app_adhoc" 2>/dev/null)
echo "ADHOC_DYLD_INSERT: $(echo "${OUT1}" | tr '\n' ',' | sed 's/,$//' | sed 's/,/, /g')"

# Interpose on Hardened Runtime binary (dyld strips DYLD_INSERT_LIBRARIES)
OUT2=$(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libhook.dylib" "${WORK_DIR}/app_hardened" 2>/dev/null)
echo "HARDENED_DYLD_INSERT: $(echo "${OUT2}" | tr '\n' ',' | sed 's/,$//' | sed 's/,/, /g')"

# Interpose on Hardened Runtime binary with allow-dyld-env and disable-library-validation entitlements
OUT3=$(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libhook.dylib" "${WORK_DIR}/app_hardened_entitled" 2>/dev/null)
echo "ENTITLED_HARDENED_DYLD_INSERT: $(echo "${OUT3}" | tr '\n' ',' | sed 's/,$//' | sed 's/,/, /g')"

# Step 4: Dyld Shared Cache virtual filesystem resolution
"${CC}" "${SCRIPT_DIR}/check_cache.c" -o "${WORK_DIR}/check_cache"
"${WORK_DIR}/check_cache"
