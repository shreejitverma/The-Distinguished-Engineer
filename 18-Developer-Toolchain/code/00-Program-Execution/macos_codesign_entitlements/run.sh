#!/usr/bin/env bash
# requires: macos
# Apple Silicon only in practice: UNSIGNED_EXEC is SIGKILL only on arm64, and the only recording is expected/darwin-arm64.txt.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

CC="${CC:-cc}"
WARN="-Wno-deprecated-declarations"

# Start a target in the background and wait (bounded) until it prints "READY <addr>".
# Sets TARGET_PID and TARGET_ADDR; the caller kills it with stop_target.
start_target() {
    local bin="$1" out="${WORK_DIR}/$(basename "$1").out"
    "${bin}" > "${out}" 2>&1 &
    TARGET_PID=$!
    local i=0
    while [ ! -s "${out}" ]; do
        sleep 0.01
        i=$((i + 1))
        if [ "${i}" -gt 1000 ]; then
            echo "target ${bin} never printed READY" >&2
            kill -9 "${TARGET_PID}" 2>/dev/null || true
            exit 1
        fi
    done
    TARGET_ADDR=$(awk '{print $2}' "${out}")
}

stop_target() {
    kill -9 "${TARGET_PID}" 2>/dev/null || true
    wait "${TARGET_PID}" 2>/dev/null || true
}

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

start_target "${WORK_DIR}/target_unentitled"
OUT_UN=$("${WORK_DIR}/tracer" "${TARGET_PID}" "${TARGET_ADDR}")
echo "TASK_FOR_PID_UNENTITLED: $(echo "${OUT_UN}" | sed 's/TASK_FOR_PID: //')"
stop_target

start_target "${WORK_DIR}/target_entitled"
OUT_EN=$("${WORK_DIR}/tracer" "${TARGET_PID}" "${TARGET_ADDR}")
echo "TASK_FOR_PID_ENTITLED: $(echo "${OUT_EN}" | grep TASK_FOR_PID | sed 's/TASK_FOR_PID: //')"
echo "${OUT_EN}" | grep "VM_READ:"
stop_target

# Step 3: PT_DENY_ATTACH is a ptrace-layer flag, separate from Mach task ports
"${CC}" ${WARN} "${SCRIPT_DIR}/attach.c" -o "${WORK_DIR}/attach"
"${CC}" ${WARN} "${SCRIPT_DIR}/traced_deny.c" -o "${WORK_DIR}/traced_deny"
"${CC}" ${WARN} "${SCRIPT_DIR}/deny_target.c" -o "${WORK_DIR}/deny_target"
codesign -s - -f --entitlements "${SCRIPT_DIR}/entitlements_task.plist" "${WORK_DIR}/deny_target" 2>/dev/null

"${WORK_DIR}/traced_deny"

# Control: the same get-task-allow target without PT_DENY_ATTACH accepts PT_ATTACH.
start_target "${WORK_DIR}/target_entitled"
"${WORK_DIR}/attach" "${TARGET_PID}" | sed 's/^PT_ATTACH:/PT_ATTACH_CONTROL:/'
stop_target

# With PT_DENY_ATTACH the attaching process is sent SIGSEGV (shell status 139); it prints nothing.
start_target "${WORK_DIR}/deny_target"
set +e
"${WORK_DIR}/attach" "${TARGET_PID}" > /dev/null 2>&1
ATTACH_STATUS=$?
set -e
if [ "${ATTACH_STATUS}" -eq 139 ]; then
    echo "PT_ATTACH_DENIED: attacher=SIGSEGV"
else
    echo "PT_ATTACH_DENIED: attacher=EXIT_${ATTACH_STATUS}"
fi
stop_target

# The same target still hands out its task port and memory.
start_target "${WORK_DIR}/deny_target"
OUT_DENY=$("${WORK_DIR}/tracer" "${TARGET_PID}" "${TARGET_ADDR}")
echo "DENY_ATTACH_TASK_FOR_PID: $(echo "${OUT_DENY}" | grep TASK_FOR_PID | sed 's/TASK_FOR_PID: //')"
echo "${OUT_DENY}" | grep "VM_READ:" | sed 's/^VM_READ:/DENY_ATTACH_VM_READ:/'
stop_target

# Step 4: Hardened Runtime and DYLD_INSERT_LIBRARIES sanitization
"${CC}" -dynamiclib "${SCRIPT_DIR}/hook.c" -o "${WORK_DIR}/libhook.dylib"
codesign -s - "${WORK_DIR}/libhook.dylib" 2>/dev/null

"${CC}" "${SCRIPT_DIR}/app.c" -o "${WORK_DIR}/app_adhoc"

"${CC}" "${SCRIPT_DIR}/app.c" -o "${WORK_DIR}/app_hardened"
codesign -s - -f --options runtime "${WORK_DIR}/app_hardened" 2>/dev/null

"${CC}" "${SCRIPT_DIR}/app.c" -o "${WORK_DIR}/app_hardened_env_only"
codesign -s - -f --options runtime --entitlements "${SCRIPT_DIR}/entitlements_dyld_env_only.plist" "${WORK_DIR}/app_hardened_env_only" 2>/dev/null

"${CC}" "${SCRIPT_DIR}/app.c" -o "${WORK_DIR}/app_hardened_entitled"
codesign -s - -f --options runtime --entitlements "${SCRIPT_DIR}/entitlements_dyld.plist" "${WORK_DIR}/app_hardened_entitled" 2>/dev/null

oneline() { tr '\n' ',' | sed 's/,$//' | sed 's/,/, /g'; }

# Interpose on standard ad-hoc binary
OUT1=$(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libhook.dylib" "${WORK_DIR}/app_adhoc" 2>/dev/null)
echo "ADHOC_DYLD_INSERT: $(echo "${OUT1}" | oneline)"

# Interpose on Hardened Runtime binary (dyld strips DYLD_INSERT_LIBRARIES)
OUT2=$(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libhook.dylib" "${WORK_DIR}/app_hardened" 2>/dev/null)
echo "HARDENED_DYLD_INSERT: $(echo "${OUT2}" | oneline)"

# Hardened Runtime plus only allow-dyld-environment-variables: dyld keeps the variable, library validation rejects the dylib
OUT_ENV=$(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libhook.dylib" "${WORK_DIR}/app_hardened_env_only" 2>/dev/null)
echo "ENV_ONLY_HARDENED_DYLD_INSERT: $(echo "${OUT_ENV}" | oneline)"

# Interpose on Hardened Runtime binary with allow-dyld-env and disable-library-validation entitlements
OUT3=$(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libhook.dylib" "${WORK_DIR}/app_hardened_entitled" 2>/dev/null)
echo "ENTITLED_HARDENED_DYLD_INSERT: $(echo "${OUT3}" | oneline)"

# Step 5: two-level namespace vs __DATA,__interpose vs -flat_namespace (does getppid() return the fake 4242?)
"${CC}" "${SCRIPT_DIR}/ppid_app.c" -o "${WORK_DIR}/ppid_app"
"${CC}" "${SCRIPT_DIR}/ppid_app.c" -Wl,-flat_namespace -o "${WORK_DIR}/ppid_app_flat"
"${CC}" -dynamiclib "${SCRIPT_DIR}/plain_hook.c" -o "${WORK_DIR}/libplain.dylib"
"${CC}" -dynamiclib "${SCRIPT_DIR}/interpose_hook.c" -o "${WORK_DIR}/libinterpose.dylib"
codesign -s - "${WORK_DIR}/libplain.dylib" "${WORK_DIR}/libinterpose.dylib" 2>/dev/null

echo "NO_INSERT: $("${WORK_DIR}/ppid_app")"
echo "TWO_LEVEL_PLAIN_DEFINITION: $(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libplain.dylib" "${WORK_DIR}/ppid_app")"
echo "TWO_LEVEL_INTERPOSE: $(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libinterpose.dylib" "${WORK_DIR}/ppid_app")"
echo "FLAT_NAMESPACE_PLAIN_DEFINITION: $(DYLD_INSERT_LIBRARIES="${WORK_DIR}/libplain.dylib" "${WORK_DIR}/ppid_app_flat")"

# Step 6: Dyld Shared Cache virtual filesystem resolution
"${CC}" "${SCRIPT_DIR}/check_cache.c" -o "${WORK_DIR}/check_cache"
"${WORK_DIR}/check_cache" LIBSYSTEM /usr/lib/libSystem.B.dylib

# libz is in the cache but is not a load command of check_cache, so a successful dlopen is a lookup by path.
if otool -L "${WORK_DIR}/check_cache" | grep -q 'libz'; then
    echo "UNLINKED_LIBZ_LINKED: YES"
else
    echo "UNLINKED_LIBZ_LINKED: NO"
fi
"${WORK_DIR}/check_cache" UNLINKED_LIBZ /usr/lib/libz.1.dylib
# Negative control: a path that is not in the cache must make dlopen fail.
"${WORK_DIR}/check_cache" ABSENT_LIB /usr/lib/libnot_in_cache.1.dylib
