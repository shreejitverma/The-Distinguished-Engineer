#!/usr/bin/env bash
# The project's build configuration: builds the C++ engine, the Rust routing object,
# and the release tarball dist/engine.tar.gz from the checkout at ROOT.
#
# Usage: build.sh ROOT FIXES
#   FIXES is a comma-separated subset of:
#     epoch   pin every clock read to SOURCE_DATE_EPOCH (compiler macros, BUILDINFO, tar, gzip)
#     prefix  map the checkout path out of every output (GCC -ffile-prefix-map, rustc --remap-path-prefix)
#     sort    make generator and archive order independent of hashing and readdir order
# With FIXES empty this is the build as it was first written, with every leak in place.
set -euo pipefail

ROOT="$1"
FIXES="${2:-}"
has() { [[ ",${FIXES}," == *",$1,"* ]]; }

cd "${ROOT}"
rm -rf gen out target dist
mkdir -p gen out target/out dist/pkg/bin dist/pkg/lib dist/pkg/include

CXXFLAGS=(-std=c++17 -O2 -g -I"${ROOT}/gen")
RUSTFLAGS=(--edition 2021 -C opt-level=2 -C codegen-units=1 -g)
GEN_ORDER=()
TAR_FLAGS=()
GZIP_FLAGS=()

if has epoch; then
    # The value is the time of the last source change, as `git log -1 --format=%ct` would give.
    SOURCE_DATE_EPOCH="$(cat src/SOURCE_DATE_EPOCH)"
    export SOURCE_DATE_EPOCH
    BUILT_AT="$(date -u -d "@${SOURCE_DATE_EPOCH}" +%Y-%m-%dT%H:%M:%SZ)"
    TAR_FLAGS+=(--mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 --numeric-owner)
    GZIP_FLAGS+=(-n)
else
    unset SOURCE_DATE_EPOCH
    BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
if has prefix; then
    CXXFLAGS+=(-ffile-prefix-map="${ROOT}"=.)
    RUSTFLAGS+=(--remap-path-prefix="${ROOT}"=.)
fi
if has sort; then
    GEN_ORDER+=(--sorted)
    TAR_FLAGS+=(--sort=name)
fi

# act NAME "INPUTS" OUTPUT CMD...: run one build action and log it to target/actions.tsv the
# way a caching executor sees it (declared inputs, output, environment, argv), so the drill
# can compute the cache key each agent would have asked for.
act() {
    local name="$1" inputs="$2" output="$3"
    shift 3
    printf '%s\t%s\t%s\tSOURCE_DATE_EPOCH=%s\t%s\n' \
        "${name}" "${inputs}" "${output}" "${SOURCE_DATE_EPOCH:-}" "$*" >> target/actions.tsv
    "$@"
}

# C++: absolute source paths, as CMake-generated build rules pass them.
act compile-version "src/version.cpp" out/version.o \
    g++ "${CXXFLAGS[@]}" -c "${ROOT}/src/version.cpp" -o out/version.o
act gen-opcodes "src/gen_opcodes.py" gen/opcodes.h \
    python3 src/gen_opcodes.py gen/opcodes.h "${GEN_ORDER[@]}"
act compile-engine "src/engine.cpp gen/opcodes.h" out/engine.o \
    g++ "${CXXFLAGS[@]}" -c "${ROOT}/src/engine.cpp" -o out/engine.o
act compile-main "src/main.cpp gen/opcodes.h" out/main.o \
    g++ "${CXXFLAGS[@]}" -c "${ROOT}/src/main.cpp" -o out/main.o
act link-engine "out/main.o out/version.o out/engine.o" out/engine \
    g++ out/main.o out/version.o out/engine.o -o out/engine
ar rcs out/libengine.a out/version.o out/engine.o

# Rust: a generator run the way a build script is, then the library as one object file.
rustc -O src/rust/gen_routes.rs -o target/gen_routes
act gen-routes "src/rust/gen_routes.rs" target/out/routes.rs \
    target/gen_routes target/out/routes.rs "${GEN_ORDER[@]}"
act compile-routing "src/rust/lib.rs target/out/routes.rs" out/routing.o \
    env OUT_DIR="${ROOT}/target/out" rustc "${RUSTFLAGS[@]}" --crate-type lib --crate-name routing \
    --emit obj -o out/routing.o src/rust/lib.rs

cp out/engine dist/pkg/bin/
cp out/libengine.a out/routing.o dist/pkg/lib/
cp gen/opcodes.h dist/pkg/include/
printf 'name=engine\nbuilt_at=%s\n' "${BUILT_AT}" > dist/pkg/BUILDINFO

tar "${TAR_FLAGS[@]}" -C dist -cf dist/engine.tar pkg
gzip "${GZIP_FLAGS[@]}" dist/engine.tar
