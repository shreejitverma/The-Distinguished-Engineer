#!/usr/bin/env bash
set -euo pipefail
# requires: macos, network

# Builds the same C abi3 extension four ways and a Rust abi3 extension once. Each build differs from
# an earlier one in exactly one variable (interpreter provenance, deployment target env var,
# interpreter version, build backend), and every interpreter is a final release. For each wheel it
# shows what the tags promise versus what the binary needs, whether CPython 3.14 on macOS 15 would
# select it, and whether it imports on 3.15 and 3.14.
#
# Beyond the harness vocabulary above, the lab needs: uv, Xcode Command Line Tools (otool), a Rust
# toolchain (rustc, cargo), and Homebrew-style non-managed CPython 3.14 and 3.15. The recorded output
# is for a macOS 27 arm64 host, where Homebrew's sysconfig target is 27; another macOS major prints
# its own number, so re-record on that host. The preflight below stops with exit 2 and names what is missing.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

need() { # need <command> <how to get it>
    command -v "$1" >/dev/null 2>&1 || { echo "wheel_tags: missing prerequisite '$1' ($2)" >&2; exit 2; }
}
need uv "https://docs.astral.sh/uv/"
need otool "xcode-select --install"
need rustc "https://rustup.rs/"
need cargo "https://rustup.rs/"
system_python() { # system_python <major.minor>
    uv python find --no-config --no-managed-python "$1" 2>/dev/null ||
        { echo "wheel_tags: no non-managed CPython $1 found (Homebrew: brew install python@$1)" >&2; exit 2; }
}

# Interpreter provenance decides the macOS floor: python-build-standalone (uv managed) is built
# with a low deployment target; Homebrew builds for the host it was built on.
MANAGED_REQ="cpython-3.14.6-macos-aarch64-none"
MANAGED_PY="$(uv python find --no-config --managed-python "${MANAGED_REQ/-macos/+gil-macos}" 2>/dev/null || {
    uv python install --no-config --no-bin -q "$MANAGED_REQ" &&
        uv python find --no-config --managed-python "${MANAGED_REQ/-macos/+gil-macos}"
})"
SYS314="$(system_python 3.14)"
SYS315="$(system_python 3.15)"

# The managed interpreter is pinned, so its full version is part of the recording; the Homebrew ones
# move with every brew upgrade, so they print major.minor only.
describe() { # describe <python> <full|short>
    "$1" -c 'import platform, sys, sysconfig
v = platform.python_version() if sys.argv[1] == "full" else "%d.%d" % sys.version_info[:2]
print("%s, sysconfig MACOSX_DEPLOYMENT_TARGET=%s" % (v, sysconfig.get_config_var("MACOSX_DEPLOYMENT_TARGET")))' "$2"
}
short_version() { # short_version <python>
    "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])'
}
echo "managed (python-build-standalone) CPython: $(describe "$MANAGED_PY" full)"
echo "system (non-managed) CPython:              $(describe "$SYS314" short)"
echo "system (non-managed) CPython:              $(describe "$SYS315" short)"
echo

# Each build gets a fresh copy of the project: setuptools reuses an existing build/ directory and
# skips the compiler, so a shared tree would silently ship the previous build's binary.
build() { # build <python> <project> <label> [env assignments...]
    local py="$1" proj="$2" label="$3"
    shift 3
    mkdir "$TMP_DIR/src-$label"
    cp -R "$SCRIPT_DIR/$proj/." "$TMP_DIR/src-$label/"
    if ! env "$@" uv build --no-config --python "$py" --wheel --out-dir "$TMP_DIR/$label" "$TMP_DIR/src-$label" >"$TMP_DIR/build.log" 2>&1; then
        cat "$TMP_DIR/build.log" >&2
        exit 1
    fi
}
build "$MANAGED_PY" c_extension c_managed
build "$SYS314" c_extension c_system
build "$SYS314" c_extension c_system_target11 MACOSX_DEPLOYMENT_TARGET=11.0
build "$SYS315" c_extension c_system315
build "$SYS315" rust_extension rust_system315

import_check() { # import_check <python> <wheel> <module>
    local venv="$TMP_DIR/venv-$RANDOM"
    if ! { uv venv --no-config -q --python "$1" "$venv" &&
        uv pip install --no-config -q --python "$venv/bin/python" "$2"; } >"$TMP_DIR/install.log" 2>&1; then
        cat "$TMP_DIR/install.log" >&2
        return 1
    fi
    if out="$("$venv/bin/python" -c "import $3" 2>&1)"; then
        echo "ok"
    else
        echo "$out" | tail -n 1 | sed -E "s#'[^']*'#'...'#g"
    fi
    rm -rf "$venv"
}

for label in c_managed c_system c_system_target11 c_system315 rust_system315; do
    wheel="$(find "$TMP_DIR/$label" -name '*.whl' | head -n 1)"
    module="c_abi3_example"
    [ "$label" = rust_system315 ] && module="rust_abi3_example"
    echo "=== $label ==="
    uv run --no-config --no-project -q --python "$SYS315" --with "packaging==25.0" \
        python "$SCRIPT_DIR/inspect_wheel.py" "$wheel" "$TMP_DIR/unz-$label"
    so="$(find "$TMP_DIR/unz-$label" -name '*.so' | head -n 1)"
    echo "Binary minimum macOS (LC_BUILD_VERSION minos): $(otool -l "$so" | awk '/LC_BUILD_VERSION/{f=1} f && /minos/{print $2; exit}')"
    if uvx --no-config -q --from "delocate==0.13.0" delocate-wheel --require-target-macos-version 11.0 \
        -w "$TMP_DIR/deloc-$label" "$wheel" >"$TMP_DIR/deloc.log" 2>&1; then
        echo "delocate --require-target-macos-version 11.0: accepted, wrote $(basename "$(find "$TMP_DIR/deloc-$label" -name '*.whl' | head -n 1)")"
    elif grep -q 'has a minimum target of' "$TMP_DIR/deloc.log"; then
        echo "delocate --require-target-macos-version 11.0: rejected"
    else
        cat "$TMP_DIR/deloc.log" >&2
        exit 1
    fi
    new_result="$(import_check "$SYS315" "$wheel" "$module")"
    old_result="$(import_check "$SYS314" "$wheel" "$module")"
    echo "Import on $(short_version "$SYS315"): $new_result"
    echo "Import on $(short_version "$SYS314"): $old_result"
    echo
done
