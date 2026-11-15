#!/usr/bin/env bash
# CMake presets, toolchain files, the configure log, and the file API, observed end to end.
# Needs: cmake >= 4.4 (presets version 12, codemodel 2.11, error index), ninja, python3, a C++ compiler.
# Runs on macOS and Linux as is; nothing here needs a requirement tag.
set -euo pipefail

LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
WORK="$(cd "${WORK}" && pwd -P)"
trap 'rm -rf "${WORK}"' EXIT

for tool in cmake ninja python3; do
    command -v "${tool}" >/dev/null || { echo "missing tool: ${tool}" >&2; exit 1; }
done
CMAKE_MM="$(cmake --version | sed -n '1s/^cmake version \([0-9]*\)\.\([0-9]*\).*/\1 \2/p')"
read -r CM_MAJOR CM_MINOR <<<"${CMAKE_MM}"
if (( CM_MAJOR < 4 || (CM_MAJOR == 4 && CM_MINOR < 4) )); then
    echo "cmake >= 4.4 required, found ${CM_MAJOR}.${CM_MINOR}" >&2
    exit 1
fi

# Isolate from the caller: no inherited toolchain, generator, build type, or user-wide file API queries.
export LC_ALL=C
unset CMAKE_TOOLCHAIN_FILE CMAKE_GENERATOR CMAKE_BUILD_TYPE CMAKE_EXPORT_COMPILE_COMMANDS CMAKE_PRESETS_FILE || true
export CMAKE_CONFIG_DIR="${WORK}/user-config"

cp -R "${LAB}/project" "${WORK}/src"
cp -R "${LAB}/broken" "${WORK}/broken"
SRC="${WORK}/src"

scrub() { sed -e "s#${WORK}#<work>#g"; }
py() { python3 -I "${LAB}/fileapi_client.py" "$@"; }
cache_get() {  # cache_get <build> <name> -> "value (TYPE)" or "(not in cache)"
    local line
    line="$(grep -E "^$2:" "$1/CMakeCache.txt" || true)"
    if [[ -z "${line}" ]]; then echo "(not in cache)"; return; fi
    local type="${line#*:}"; type="${type%%=*}"
    echo "${line#*=} (${type})"
}
expect_fail() {  # expect_fail <label> <cmd...>: the command must fail; print its stderr
    local label="$1"; shift
    if "$@" >"${WORK}/out.txt" 2>"${WORK}/err.txt"; then
        echo "UNEXPECTED SUCCESS: ${label}" >&2; exit 1
    fi
    echo "[${label}] exit status non-zero"
    scrub <"${WORK}/err.txt" | sed -e 's/^/    /'
}
bad_presets() {  # bad_presets <name> <json>: a project whose only defect is its presets file
    mkdir -p "${WORK}/$1"
    printf 'cmake_minimum_required(VERSION 3.25)\nproject(Bad NONE)\n' >"${WORK}/$1/CMakeLists.txt"
    printf '%s\n' "$2" >"${WORK}/$1/CMakePresets.json"
}

# File API queries must exist before the first configure of a build tree.
DEV="${WORK}/out/dev"
mkdir -p "${DEV}/.cmake/api/v1/query/client-lab" "${WORK}/out/cross/.cmake/api/v1/query"
touch "${DEV}/.cmake/api/v1/query/codemodel-v2" "${DEV}/.cmake/api/v1/query/bogus-v1"
cat >"${DEV}/.cmake/api/v1/query/client-lab/query.json" <<'EOF'
{
  "requests": [
    { "kind": "toolchains", "version": [3, 1] },
    { "kind": "cache", "version": 2 },
    { "kind": "configureLog", "version": { "major": 1, "minor": 0 } },
    { "kind": "nope", "version": 1 }
  ],
  "client": { "lab": "cmake_file_api" }
}
EOF
touch "${WORK}/out/cross/.cmake/api/v1/query/codemodel-v2"

echo "== 1. Presets (cmake ${CM_MAJOR}.${CM_MINOR})"
cd "${SRC}"
cmake --list-presets=all
echo "(windows-only is absent: its condition is false on this host)"
echo

cmake --preset dev >/dev/null
cmake --preset rel -DLAB_OPT=from-command-line >/dev/null
cmake --preset cross >/dev/null
for p in dev rel cross; do
    b="${WORK}/out/${p}"
    echo "preset ${p} -> binaryDir <work>/out/${p}"
    for v in CMAKE_BUILD_TYPE LAB_TAG LAB_TYPED LAB_OPT; do
        printf '  %-16s = %s\n' "${v}" "$(cache_get "${b}" "${v}")"
    done
done
echo

if [[ "$(cat "${DEV}/lab-env.txt")" == "/opt/lab/bin:${PATH}" ]]; then
    echo "environment: configure saw LAB_PATH = /opt/lab/bin + ':' + parent PATH (\$penv{PATH}): yes"
else
    echo "environment: LAB_PATH mismatch" >&2; exit 1
fi
echo "environment: LAB_PATH stored in CMakeCache.txt: $(grep -c '^LAB_PATH' "${DEV}/CMakeCache.txt" || true) entries"
echo

expect_fail "cmake --preset windows-only" cmake --preset windows-only
# 99 stays above MAX_VERSION for years; a real next format (13 arrived in CMake 4.5) would turn this into a success.
bad_presets bad-version '{"version": 99, "configurePresets": [{"name": "a"}]}'
expect_fail "presets version 99" bash -c "cd '${WORK}/bad-version' && cmake --preset a"
bad_presets bad-cycle '{"version": 6, "configurePresets": [{"name": "a", "inherits": "b", "binaryDir": "x"}, {"name": "b", "inherits": "a"}]}'
expect_fail "inheritance cycle" bash -c "cd '${WORK}/bad-cycle' && cmake --preset a"
bad_presets bad-dup '{"version": 6, "configurePresets": [{"name": "a", "binaryDir": "x"}]}'
printf '%s\n' '{"version": 6, "configurePresets": [{"name": "a", "binaryDir": "y"}]}' >"${WORK}/bad-dup/CMakeUserPresets.json"
expect_fail "same name in CMakePresets.json and CMakeUserPresets.json" bash -c "cd '${WORK}/bad-dup' && cmake --preset a"
bad_presets bad-min '{"version": 6, "cmakeMinimumRequired": {"major": 5}, "configurePresets": [{"name": "a", "binaryDir": "x"}]}'
expect_fail "cmakeMinimumRequired 5.0" bash -c "cd '${WORK}/bad-min' && cmake --preset a"
echo

STICKY="${WORK}/sticky"
cmake --preset dev -B "${STICKY}" -DLAB_STICKY=1 >/dev/null
echo "cache stickiness (preset dev, -B <work>/sticky overrides binaryDir):"
echo "  after 'cmake --preset dev -DLAB_STICKY=1':  LAB_STICKY = $(cache_get "${STICKY}" LAB_STICKY)"
cmake --preset dev -B "${STICKY}" >/dev/null
echo "  after 'cmake --preset dev' (no -D):         LAB_STICKY = $(cache_get "${STICKY}" LAB_STICKY)"
cmake --preset dev -B "${STICKY}" --fresh >/dev/null
echo "  after 'cmake --preset dev --fresh':         LAB_STICKY = $(cache_get "${STICKY}" LAB_STICKY)"
echo

echo "workflow preset dev:"
cmake --workflow dev 2>&1 | grep -E '^Executing workflow step|tests passed' | sed -e 's/^/  /'
echo

echo "== 2. Toolchain files"
export LAB_TC_LOG="${WORK}/tc.log"
summarize_tc() { sort "${LAB_TC_LOG}" | uniq -c | sed -e 's/^ */  /'; : >"${LAB_TC_LOG}"; }
: >"${LAB_TC_LOG}"
cmake -S "${SRC}" -B "${WORK}/tc-a" -G Ninja --toolchain "${SRC}/toolchains/counting.cmake" -DLAB_TC_MODE=alpha >/dev/null
echo "reads of counting.cmake, fresh configure (count, context, LAB_TC_MODE seen):"
summarize_tc
cmake -S "${SRC}" -B "${WORK}/tc-a" >/dev/null
echo "reads on re-configure of the same tree:"
summarize_tc
SYS="$(ls "${WORK}"/tc-a/CMakeFiles/*/CMakeSystem.cmake)"
echo "CMakeSystem.cmake re-includes the toolchain: $(grep '^include(' "${SYS}" | scrub)"
cmake -S "${SRC}" -B "${WORK}/tc-b" -G Ninja --toolchain "${SRC}/toolchains/counting-propagate.cmake" -DLAB_TC_MODE=alpha >/dev/null
echo "reads with CMAKE_TRY_COMPILE_PLATFORM_VARIABLES=LAB_TC_MODE:"
summarize_tc
echo

CSYS="$(ls "${WORK}"/out/cross/CMakeFiles/*/CMakeSystem.cmake)"
host="$(sed -n 's/^set(CMAKE_HOST_SYSTEM_NAME "\(.*\)")$/\1/p' "${CSYS}")"
target="$(sed -n 's/^set(CMAKE_SYSTEM_NAME "\(.*\)")$/\1/p' "${CSYS}")"
echo "preset cross: CMAKE_SYSTEM_NAME equals CMAKE_HOST_SYSTEM_NAME: $([[ "${host}" == "${target}" ]] && echo yes || echo no)"
echo "preset cross: $(grep 'CMAKE_CROSSCOMPILING' "${CSYS}")"
echo "preset dev:   $(grep 'CMAKE_CROSSCOMPILING' "$(ls "${DEV}"/CMakeFiles/*/CMakeSystem.cmake)")"
py launchers "${WORK}/out/cross"
export LAB_EMU_LOG="${WORK}/emu.log"
: >"${LAB_EMU_LOG}"
cmake --build --preset cross >/dev/null
ctest --preset cross 2>&1 | grep -E 'tests passed' | sed -e 's/^/  cross ctest: /'
echo "  emulator log: $(cat "${LAB_EMU_LOG}")"
: >"${LAB_EMU_LOG}"
ctest --preset dev 2>&1 | grep -E 'tests passed' | sed -e 's/^/  dev ctest:   /'
echo "  emulator log after dev ctest: $(wc -l <"${LAB_EMU_LOG}" | tr -d ' ') lines"
echo

echo "== 3. File API (build tree of preset dev)"
py report "${DEV}"
py names "${DEV}"
echo

py list "${DEV}" >"${WORK}/before.txt"
cmake --preset dev >/dev/null
echo "re-configure with no changes:"
py changed "${WORK}/before.txt" "${DEV}"
py list "${DEV}" >"${WORK}/before.txt"
cmake --preset dev -DLAB_LEVEL=4 >/dev/null
echo "re-configure with -DLAB_LEVEL=4 (changes one compile definition):"
py changed "${WORK}/before.txt" "${DEV}"
py names "${DEV}"
echo "index files present: $(ls "${DEV}/.cmake/api/v1/reply" | grep -c '^index-')"
echo

echo "== 4. Error index (CMake >= 4.1)"
BROKEN="${WORK}/broken-build"
mkdir -p "${BROKEN}/.cmake/api/v1/query"
touch "${BROKEN}/.cmake/api/v1/query/codemodel-v2" "${BROKEN}/.cmake/api/v1/query/configureLog-v1"
cmake -S "${WORK}/broken" -B "${BROKEN}" -G Ninja >/dev/null
echo "after a successful configure:"
py error "${BROKEN}"
if cmake -S "${WORK}/broken" -B "${BROKEN}" -DLAB_BREAK=ON >/dev/null 2>"${WORK}/err.txt"; then
    echo "UNEXPECTED SUCCESS: LAB_BREAK=ON" >&2; exit 1
fi
# Fail the lab if configure failed for any reason other than the one under test.
grep -q 'lab: forced configure failure' "${WORK}/err.txt" || { cat "${WORK}/err.txt" >&2; exit 1; }
echo "after a failed configure (-DLAB_BREAK=ON):"
py error "${BROKEN}"
