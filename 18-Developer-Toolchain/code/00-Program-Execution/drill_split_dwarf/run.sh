#!/usr/bin/env bash
# requires: linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
SERVER_PID=""
cleanup() {
    if [ -n "${SERVER_PID}" ]; then
        kill "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT
cd "${WORK_DIR}"

CXX="${CXX:-g++}"
PORT=8765

# Build from a copy of the source in a fresh work directory, so the recorded DW_AT_name stays "crash.cpp".
cp "${SCRIPT_DIR}/crash.cpp" .
mkdir symbols cache

echo "=== 1. BUILD STRIPPED BINARY AND SPLIT DWARF PACKAGE ==="
# -fdebug-prefix-map pins DW_AT_comp_dir, so the dwo_id (a hash that covers it) does not depend on the build directory.
# -gdwarf-4 selects the GNU Split DWARF extension (DW_AT_GNU_dwo_*, package index version 2).
"${CXX}" -O2 -g -gdwarf-4 -gsplit-dwarf \
    -fdebug-prefix-map="${WORK_DIR}=/lab" \
    -c crash.cpp -o crash.o

# Deterministic Build ID, set at link time only (the -c step does not link).
"${CXX}" crash.o \
    -Wl,--build-id=0x11223344556677889900aabbccddeeff11223344 \
    -o crash_prog

# Package the split DWARF objects, then delete the .dwo so only the package can satisfy GDB later.
dwp -o crash_prog.dwp crash.dwo
rm crash.dwo

# Keep the skeleton, .debug_addr and symbol table in a .debug file, then strip the production executable.
objcopy --only-keep-debug crash_prog crash_prog.debug
strip --strip-all crash_prog

BUILD_ID=$(readelf -n crash_prog | awk '/Build ID:/ {print $3}')
echo "Build ID: ${BUILD_ID}"

echo "--- Sections in stripped production binary ---"
readelf -S -W crash_prog | grep -oE '\.(text|eh_frame|eh_frame_hdr|symtab|strtab|debug_[a-z_.]*)' | sort -u

echo "--- Sections in detached Split DWARF package (.dwp) ---"
readelf -S -W crash_prog.dwp | grep -o '\.debug_[a-z_.]*'

echo "--- Sections in the .debug file (skeleton and .debug_addr) ---"
# readelf complains that the NOBITS .debug file has no program interpreter; that is expected and unrelated.
readelf -S -W crash_prog.debug 2>/dev/null | grep -oE '\.(symtab|debug_[a-z_.]*)' | sort -u
echo "--- Section type of .text in the .debug file (NOBITS means code bytes were dropped) ---"
readelf -S -W crash_prog.debug 2>/dev/null | sed -E 's/\[ *[0-9]+\]/[N]/' | awk '$2 == ".text" {print $2, $3}'

echo ""
echo "=== 2. CRASH SYMPTOM ON STRIPPED BINARY ==="
# No DEBUGINFOD_URLS and no .debug file next to the binary: this is the bare production view.
env -u DEBUGINFOD_URLS gdb -q -batch -ex "r" -ex "bt" ./crash_prog 2>&1 | \
    grep -E "(Program received signal|#[0-9]+)" | \
    sed -E 's/0x[0-9a-fA-F]+/0xADDR/g'

echo ""
echo "=== 3. PHYSICAL FRAME RECONSTRUCTION VIA .EH_FRAME ==="
# The stripped binary has no symbols, so name the functions from the .debug file's symbol table,
# then print the complete FDE (every opcode, no truncation) for each function of interest.
# Paragraph mode: readelf separates entries with blank lines.
readelf -wf crash_prog > eh_frame.txt
fde() {
    local addr
    addr=$(nm -n crash_prog.debug | awk -v s="$1" '$3 == s {print $1}')
    echo "--- FDE for $2 (pc=${addr}..) ---"
    awk -v pat="pc=${addr}\\\\.\\\\." 'BEGIN { RS = ""; ORS = "\n" } $0 ~ pat { print }' eh_frame.txt
}
fde main main
fde _Z15physical_workerPVi physical_worker
fde __do_global_dtors_aux __do_global_dtors_aux
echo "--- CIE shared by all FDEs ---"
awk 'BEGIN { RS = ""; ORS = "\n" } /CIE/ { print; exit }' eh_frame.txt
rm eh_frame.txt

echo ""
echo "=== 4. SPLIT DWARF PACKAGE (.DWP) CU INDEX ==="
# readelf exits non-zero here (the empty .debug_tu_index warns); record the status instead of hiding it.
readelf --debug-dump=cu_index crash_prog.dwp > cu_index.txt 2> readelf.err || echo "readelf exit status: $?"
grep -vE '^$|Contents of' cu_index.txt
echo "--- readelf stderr (unique) ---"
sort -u readelf.err
rm cu_index.txt

echo ""
echo "=== 5. INLINED SUBROUTINE DIE RESOLUTION ==="
# No head: show every inlined DIE, its abstract origin, call line, and the abstract names.
readelf -wi crash_prog.dwp > info.txt 2> readelf.err || echo "readelf exit status: $?"
grep -E "(DW_TAG_inlined_subroutine|DW_AT_abstract_origin|DW_AT_call_line|DW_AT_name .*level[1-3])" info.txt
echo "--- readelf stderr (unique) ---"
sort -u readelf.err
rm info.txt readelf.err

echo ""
echo "=== 6. DEBUGINFOD SYMBOL RETRIEVAL AND FULL STACK UNWINDING ==="
cp crash_prog.debug "symbols/${BUILD_ID}.debug"

# Start the stand-in debuginfod server and wait until it accepts connections.
python3 "${SCRIPT_DIR}/debuginfod_server.py" > server.log 2>&1 &
SERVER_PID=$!
python3 - "${PORT}" <<'PY'
import socket, sys, time
port = int(sys.argv[1])
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.1)
sys.exit("debuginfod stand-in never started")
PY

# A private client cache keeps the run hermetic: GDB must fetch, not reuse an earlier download.
export DEBUGINFOD_CACHE_PATH="${WORK_DIR}/cache"
export DEBUGINFOD_URLS="http://127.0.0.1:${PORT}"

# Normalize everything that depends on the work directory or on ASLR before printing.
squash() {
    sed -E -e 's/0x[0-9a-fA-F]+/0xADDR/g' -e 's#\[in module [^]]*\]#[in module DEBUGFILE]#'
}
run_gdb() { # $@ = extra gdb commands run before "file"
    gdb -q -batch -ex "set debuginfod enabled on" -ex "set debuginfod verbose 0" "$@" \
        -ex "file ./crash_prog" -ex "r" -ex "bt" 2>&1
}

echo "--- 6a. debuginfod serves the .debug file; crash_prog.dwp sits next to the executable ---"
test -f crash_prog.dwp && echo "crash_prog.dwp next to crash_prog: yes"
run_gdb | grep -E "(Program received signal|#[0-9]+|^warning: Could not find DWO CU)" | squash
echo "--- requests the debuginfod stand-in served ---"
grep SERVE server.log | sed -E 's/, [0-9]+ bytes//'

echo "--- 6b. the same .dwp copied next to the fetched debug file ---"
# The client cache keys the fetched file by Build ID; GDB also asks for libc and the loader, which the stand-in 404s.
DEBUG_DIR="cache/${BUILD_ID}"
test -s "${DEBUG_DIR}/debuginfo"
cp crash_prog.dwp "${DEBUG_DIR}/crash_prog.dwp"
run_gdb -ex "set debug dwarf-read 1" | grep -E "open_and_init_dwp_file: DWP file" | \
    sed -E 's#: [^ ]*/([^/ ]+)$#: <fetched debug file dir>/\1#' | sort -u
run_gdb | grep -E "(Program received signal|#[0-9]+|^warning: Could not find DWO CU)" | squash
