#!/usr/bin/env bash
# Meson lab: typed DSL errors, WrapDB fallback and its trust chain,
# declare_dependency propagation, unity-build block assignment, and a
# build/host split with a cross file.
# requires: macos, network
#
# macos:   the cross step targets x86_64-apple-darwin with Apple clang's -arch flag
#          and reads the result with lipo.
# network: the WrapDB step downloads zlib 1.3.1 (wrap zlib_1.3.1-3) once.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C
# Keep the host's compiler wrappers (ccache, sccache) and flags out of the recording.
unset CFLAGS LDFLAGS CPPFLAGS PKG_CONFIG_PATH
export CC="${CC:-cc}"

for tool in meson ninja "${CC}" lipo otool shasum python3; do
    command -v "${tool}" >/dev/null 2>&1 || { echo "missing tool: ${tool}" >&2; exit 2; }
done

echo "meson $(meson --version), ninja $(ninja --version)"

# Print the first line matching a pattern from a log, with the work dir removed.
first_match() { grep -m1 -E "$1" "$2" | sed -e "s#${WORK}/##g" -e 's#/private##g'; }

stage() { rm -rf "${WORK}/$2"; cp -R "${SCRIPT_DIR}/projects/$1" "${WORK}/$2"; }

cd "${WORK}"

echo
echo "=== 1. The DSL is typed and checked at configure time ==="
stage dsl_ok dsl_ok
meson setup dsl_ok/b dsl_ok >dsl_ok.log 2>&1
grep -E '^Message: ' dsl_ok.log
for p in dsl_typeerr dsl_kwarg; do
    stage "${p}" "${p}"
    if meson setup "${p}/b" "${p}" >"${p}.log" 2>&1; then
        echo "${p}: configure unexpectedly succeeded"; exit 1
    fi
    first_match 'ERROR:' "${p}.log"
done

echo
echo "=== 2. WrapDB fallback, the package cache, and sha256 pinning ==="
CACHE="${WORK}/cache"
mkdir -p "${CACHE}"
export MESON_PACKAGE_CACHE_DIR="${CACHE}"
# --force-fallback-for=zlib makes the result independent of any system zlib.
FF=(--force-fallback-for=zlib)

stage app app_nodl
if meson setup app_nodl/b app_nodl "${FF[@]}" --wrap-mode=nodownload >nodl.log 2>&1; then
    echo "nodownload with an empty cache unexpectedly succeeded"; exit 1
fi
echo "nodownload, empty cache: $(first_match 'ERROR: Automatic' nodl.log | sed 's/.*ERROR: /ERROR: /')"

stage app app
meson setup app/b app "${FF[@]}" >setup.log 2>&1
first_match 'Run-time dependency mylib found' setup.log
first_match 'Using subprojects/mylib/subprojects/zlib.wrap' setup.log | sed 's/^mylib| /mylib| /'
first_match 'Building fallback subproject with' setup.log
first_match 'Dependency zlib for host machine found' setup.log
first_match '^Dependency mylib for host machine found' setup.log
echo "main subprojects/zlib.wrap written by Meson:"
sed 's/^/  /' app/subprojects/zlib.wrap
echo "cache holds: $(cd "${CACHE}" && ls | sort | paste -sd " " -)"
echo "cached source sha256 matches wrap: $( [ "$(shasum -a 256 "${CACHE}/zlib-1.3.1.tar.gz" | cut -d' ' -f1)" = "$(sed -n 's/^source_hash = //p' app/subprojects/mylib/subprojects/zlib.wrap)" ] && echo yes || echo no)"
echo "hash of wrap recorded in subprojects/zlib-1.3.1/.meson-subproject-wrap-hash.txt: $( [ "$(cat app/subprojects/zlib-1.3.1/.meson-subproject-wrap-hash.txt)" = "$(shasum -a 256 app/subprojects/mylib/subprojects/zlib.wrap | cut -d' ' -f1)" ] && echo 'sha256 of the wrap file' || echo other)"

ninja -C app/b >build.log 2>&1
./app/b/app
if otool -L app/b/app | grep -q 'libz'; then echo "app links a libz dylib: yes"; else echo "app links a libz dylib: no (static fallback)"; fi

stage app app_offline
meson setup app_offline/b app_offline "${FF[@]}" --wrap-mode=nodownload >offline.log 2>&1
echo "nodownload, warm cache: $(first_match 'zlib source from cache' offline.log)"

# Tamper with one cached byte: the sha256 in the wrap must reject it.
TCACHE="${WORK}/tcache"
cp -R "${CACHE}" "${TCACHE}"
printf 'X' >>"${TCACHE}/zlib-1.3.1.tar.gz"
stage app app_tamper
if MESON_PACKAGE_CACHE_DIR="${TCACHE}" meson setup app_tamper/b app_tamper "${FF[@]}" --wrap-mode=nodownload >tamper.log 2>&1; then
    echo "tampered cache unexpectedly accepted"; exit 1
fi
echo "tampered cache: $(first_match 'ERROR: Incorrect hash' tamper.log | sed 's/.*ERROR: /ERROR: /')"

# Edit the wrap after the first configure: Meson only warns.
printf '\n# local edit\n' >>app/subprojects/mylib/subprojects/zlib.wrap
meson setup --reconfigure app/b app "${FF[@]}" >reconf.log 2>&1
echo "edited wrap on reconfigure: $(first_match 'out of date' reconf.log | sed 's/.*WARNING: /WARNING: /')"

echo
echo "=== 3. A dead primary URL: retries, fallback URL, and leftovers ==="
FCACHE="${WORK}/fcache"
mkdir -p "${FCACHE}"
stage app app_dead
sed -i '' 's#^source_url = .*#source_url = https://github.com/mesonbuild/wrapdb/releases/download/zlib_0.0.0-0/missing.tar.gz#' \
    app_dead/subprojects/mylib/subprojects/zlib.wrap
MESON_PACKAGE_CACHE_DIR="${FCACHE}" meson setup app_dead/b app_dead "${FF[@]}" >dead.log 2>&1
echo "retry warnings before giving up on source_url: $(grep -c 'failed to download with error' dead.log)"
echo "fallback used: $(grep -c 'Downloading zlib source from https://github.com/mesonbuild/wrapdb/releases/download/zlib_1.3.1-3/' dead.log)"
echo "temporary files left in the package cache: $(cd "${FCACHE}" && ls | grep -c '^tmp' || true)"

echo
echo "=== 4. Unity builds: block assignment decides whether static names clash ==="
for cfg in "off 4" "on 4" "on 2"; do
    set -- ${cfg}
    stage unity "u_$1_$2"
    meson setup "u_$1_$2/b" "u_$1_$2" -Dunity="$1" -Dunity_size="$2" >"u_$1_$2.setup.log" 2>&1
    n=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "u_$1_$2/b/compile_commands.json")
    blocks=""
    for f in $(ls "u_$1_$2/b/u.p/" 2>/dev/null | grep -E '^u-unity[0-9]+\.c$' | sort); do
        blocks="${blocks} ${f}=[$(sed -e 's#.*/##' -e 's#>##' "u_$1_$2/b/u.p/${f}" | tr '\n' ' ' | sed 's/ $//')]"
    done
    if ninja -C "u_$1_$2/b" >"u_$1_$2.build.log" 2>&1; then
        result="builds, $("./u_$1_$2/b/u")"
    else
        result="fails: $(grep -m1 -oE "redefinition of '[a-z_]+'" "u_$1_$2.build.log")"
    fi
    echo "unity=$1 unity_size=$2: compile commands=${n}${blocks:+;}${blocks}; ${result}"
done

echo
echo "=== 5. Cross file: build machine runs the generator, host machine runs the app ==="
stage cross cross
meson setup cross/b cross --cross-file cross/x86_64-darwin.ini >cross.log 2>&1
grep -E '^Message: ' cross.log
ninja -C cross/b >cross.build.log 2>&1
echo "gen (native: true) arch: $(lipo -archs cross/b/gen)"
echo "app (host) arch: $(lipo -archs cross/b/app)"
stage cross cross2
meson setup cross2/b cross2 --cross-file cross2/x86_64-darwin.ini --cross-file cross2/runs-under-rosetta.ini >cross2.log 2>&1
echo "with a second cross file setting needs_exe_wrapper = false:"
grep -E '^Message: is_cross' cross2.log
