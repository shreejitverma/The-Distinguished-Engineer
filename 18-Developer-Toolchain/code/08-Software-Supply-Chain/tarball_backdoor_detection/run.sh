#!/usr/bin/env bash
# requires: linux
# Drill lab: detect a release tarball whose build script differs from `git archive` of the tag, then show the
# ELF-level footprint of the injected IFUNC resolver.  Needs GNU make, a C compiler on glibc, GNU base64 and tr,
# binutils readelf, git and python3.  Everything it builds is benign.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

export LC_ALL=C
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export TZ=UTC
for tool in git python3 base64 tr readelf awk tar "${CC:-cc}"; do
    command -v "${tool}" >/dev/null 2>&1 || { echo "missing required tool: ${tool}" >&2; exit 127; }
done
PREFIX="libtoyz-1.0.0"
TAG="v1.0.0"

echo "# Stage 0: scenario"
python3 -I "${HERE}/scenario.py" "${WORK}"

echo
echo "# Stage 1: audit the release tarball against git archive of ${TAG}"
rc=0
python3 -I "${HERE}/tarball_audit.py" "release tarball" "${WORK}/${PREFIX}.tar.gz" "${WORK}/repo" "${TAG}" "${PREFIX}" --naive || rc=$?
echo "audit exit status: ${rc}"

echo
echo "# Stage 2: negative control, a tarball with only generated extras"
rc=0
python3 -I "${HERE}/tarball_audit.py" "clean control" "${WORK}/${PREFIX}-clean.tar.gz" "${WORK}/repo" "${TAG}" "${PREFIX}" || rc=$?
echo "audit exit status: ${rc}"

echo
echo "# Stage 3: build three trees"
mkdir -p "${WORK}/b-git" "${WORK}/b-dormant" "${WORK}/b-trigger"
git -C "${WORK}/repo" archive --format=tar --prefix="${PREFIX}/" "${TAG}" | tar -x -C "${WORK}/b-git"
tar -xzf "${WORK}/${PREFIX}.tar.gz" -C "${WORK}/b-dormant"
tar -xzf "${WORK}/${PREFIX}.tar.gz" -C "${WORK}/b-trigger"
build() {  # build DIR [env assignment]
    local dir="$1"; shift
    if ! env "$@" make -C "${WORK}/${dir}/${PREFIX}" CC="${CC:-cc}" >"${WORK}/${dir}.log" 2>&1; then
        cat "${WORK}/${dir}.log" >&2
        echo "build failed: ${dir}" >&2
        exit 1
    fi
}
build b-git PKG_BUILD=0
build b-dormant PKG_BUILD=0
build b-trigger PKG_BUILD=1
GIT_LIB="${WORK}/b-git/${PREFIX}/libtoyz.so"
DORMANT_LIB="${WORK}/b-dormant/${PREFIX}/libtoyz.so"
TRIGGER_LIB="${WORK}/b-trigger/${PREFIX}/libtoyz.so"
echo "built: git tree (PKG_BUILD=0), tarball tree dormant (PKG_BUILD=0), tarball tree armed (PKG_BUILD=1)"
if cmp -s "${WORK}/b-git/${PREFIX}/src/crc_fast.c" "${WORK}/b-trigger/${PREFIX}/src/crc_fast.c"; then
    echo "src/crc_fast.c after the armed build matches the tag: yes"
else
    echo "src/crc_fast.c after the armed build matches the tag: NO (the build rewrote a tracked source file)"
fi
if cmp -s "${GIT_LIB}" "${DORMANT_LIB}"; then
    echo "dormant tarball build byte-identical to git build: yes"
else
    echo "dormant tarball build byte-identical to git build: no"
fi

echo
echo "# Stage 4: ELF diff, git build versus armed tarball build"
echo "git build:"
python3 -I "${HERE}/elf_ifunc.py" summary "${GIT_LIB}"
echo "armed tarball build:"
python3 -I "${HERE}/elf_ifunc.py" summary "${TRIGGER_LIB}"
python3 -I "${HERE}/elf_ifunc.py" compare "${GIT_LIB}" "${TRIGGER_LIB}"
for which in git trigger; do
    lib="${GIT_LIB}"; [ "${which}" = trigger ] && lib="${TRIGGER_LIB}"
    n="$(readelf --dyn-syms -W "${lib}" | awk '$4 == "IFUNC"' | wc -l | tr -d ' ')"
    echo "readelf cross-check, IFUNC dynsym entries in ${which} build: ${n}"
done

echo
echo "# Stage 5: behavior"
"${CC:-cc}" -O1 -o "${WORK}/driver" "${HERE}/driver.c" -ldl
for which in git dormant trigger; do
    case "${which}" in
        git) lib="${GIT_LIB}" ;;
        dormant) lib="${DORMANT_LIB}" ;;
        trigger) lib="${TRIGGER_LIB}" ;;
    esac
    echo "${which}:"
    "${WORK}/driver" "${lib}"
done
