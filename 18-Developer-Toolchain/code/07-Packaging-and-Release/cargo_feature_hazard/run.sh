#!/usr/bin/env bash
# Cargo feature unification hazard: a std feature leaks into a no_std firmware build.
# Builds one small workspace (shared, firmware, host-tool) under several resolver
# settings and manifest shapes, and prints which builds of `shared` get `std`.
# Needs stable cargo 1.84 or newer (resolver "3"); no network, path dependencies only.
# No requirements line: the lab runs on macOS and Linux hosts as is.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

command -v cargo >/dev/null || { echo "cargo not found" >&2; exit 1; }

# Isolate from the caller's Cargo configuration and environment.
export CARGO_HOME="${WORK}/cargo-home"
export CARGO_TERM_COLOR=never
export CARGO_TERM_PROGRESS_WHEN=never
unset RUSTFLAGS CARGO_ENCODED_RUSTFLAGS CARGO_BUILD_TARGET CARGO_TARGET_DIR CARGO_BUILD_RUSTFLAGS
export LC_ALL=C

TRIPWIRE='feature leak: shared was compiled with std'
FW_FLAGS='--cfg firmware_target'   # stands in for target_os = "none"
MEMBERS='members = ["shared", "firmware", "host-tool"]'

# new_ws NAME ROOT_MANIFEST_BODY: fresh copy of the workspace with the given root Cargo.toml.
new_ws() {
    local dir="${WORK}/$1"
    cp -R "${LAB_DIR}/ws" "${dir}"
    printf '%s\n' "$2" >"${dir}/Cargo.toml"
    echo "${dir}"
}

# strip_paths: drop " (/abs/path)" suffixes that cargo prints for path dependencies.
strip_paths() { sed -E 's# \((/|[A-Za-z]:)[^)]*\)##'; }

# shared_features DIR EDGES [cargo tree args]: features enabled on shared, as cargo tree resolves them.
shared_features() {
    local dir="$1" edges="$2"; shift 2
    (cd "${dir}" && cargo tree --offline -q "$@" -e "${edges}" -i shared \
        -f '{p} [{f}]' --prefix none --depth 0 2>/dev/null) | strip_paths | sort -u | tr '\n' ' ' | sed 's/ $//'
    echo
}

# fw_build DIR LABEL [cargo build args]: build with the firmware cfg and classify the result.
fw_build() {
    local dir="$1" label="$2" out rc; shift 2
    set +e
    out="$(cd "${dir}" && RUSTFLAGS="${FW_FLAGS}" cargo build --offline "$@" 2>&1)"
    rc=$?
    set -e
    if [[ ${rc} -eq 0 ]]; then
        echo "${label}: ok"
    elif grep -q "${TRIPWIRE}" <<<"${out}"; then
        echo "${label}: FAILED (tripwire: shared has std)"
    else
        echo "${label}: FAILED (unexpected error)"
        printf '%s\n' "${out}" >&2
        exit 1
    fi
    LAST_OUT="${out}"
}

# shared_units DIR [cargo build args]: number of distinct compilations of the shared library.
shared_units() {
    local dir="$1"; shift
    (cd "${dir}" && cargo build --offline -q --message-format=json "$@" 2>/dev/null) |
        python3 -c '
import json, sys
units = set()
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("reason") == "compiler-artifact" and msg["target"]["name"] == "shared":
        units.add((tuple(sorted(msg["features"])), tuple(msg["filenames"])))
for feats, _ in sorted(units):
    print("  shared built with features [" + ",".join(feats) + "]")
print("  distinct shared compilations: %d" % len(units))
'
}

echo "=== 1. Incident: resolver = \"1\", dev-dependency enables std ==="
D="$(new_ws r1 "[workspace]
${MEMBERS}
resolver = \"1\"")"
printf 'shared features for cargo build -p firmware (cargo tree -e normal,build): %s\n' "$(shared_features "${D}" normal,build -p firmware)"
fw_build "${D}" "cargo build -p firmware" -p firmware
echo "first rustc error line: $(grep -m1 '^error' <<<"${LAST_OUT}")"
echo "inverted feature graph (cargo tree -p firmware -e features -i shared):"
(cd "${D}" && cargo tree --offline -q -p firmware -e features -i shared --charset ascii) | strip_paths | sed 's/^/  /'

echo
echo "=== 2. Implicit resolver: virtual workspace with no resolver key ==="
D="$(new_ws implicit "[workspace]
${MEMBERS}")"
set +e
WARN="$(cd "${D}" && cargo tree --offline -p firmware -e normal 2>&1 >/dev/null | grep -m1 'resolver')"
set -e
echo "cargo warning: $(sed -E 's/^warning: //' <<<"${WARN}" | strip_paths)"
fw_build "${D}" "cargo build -p firmware" -p firmware

echo
echo "=== 3. Fix A: resolver = \"2\" ==="
D="$(new_ws r2 "[workspace]
${MEMBERS}
resolver = \"2\"")"
printf 'shared features, cargo tree -e normal,build (what cargo build sees): %s\n' "$(shared_features "${D}" normal,build -p firmware)"
printf 'shared features, cargo tree default edges (includes dev, what cargo test sees): %s\n' "$(shared_features "${D}" normal,build,dev -p firmware)"
fw_build "${D}" "cargo build -p firmware" -p firmware
set +e
TEST_OUT="$(cd "${D}" && cargo test --offline -q -p firmware 2>&1)"
TEST_RC=$?
set -e
echo "cargo test -p firmware (host, std helper used by the unit test): $([[ ${TEST_RC} -eq 0 ]] && echo ok || echo FAILED)"
grep -m1 '^test result' <<<"${TEST_OUT}" | sed -E 's/; finished in .*//; s/^/  /'

echo
echo "=== 4. Remaining hazard under resolver 2: one invocation selects firmware and host-tool ==="
fw_build "${D}" "cargo build --workspace" --workspace
fw_build "${D}" "cargo build -p firmware -p host-tool" -p firmware -p host-tool
printf 'shared features, cargo tree --workspace -e normal,build: %s\n' "$(shared_features "${D}" normal,build --workspace)"
fw_build "${D}" "separate invocation: cargo build -p firmware" -p firmware
fw_build "${D}" "separate invocation: cargo build -p host-tool" -p host-tool

echo
echo "=== 5. resolver = \"3\" keeps the resolver 2 feature rules ==="
D="$(new_ws r3 "[workspace]
${MEMBERS}
resolver = \"3\"")"
printf 'shared features for cargo build -p firmware: %s\n' "$(shared_features "${D}" normal,build -p firmware)"
fw_build "${D}" "cargo build -p firmware" -p firmware
fw_build "${D}" "cargo build --workspace" --workspace

echo
echo "=== 6. Build-dependency path: firmware/build.rs uses shared with std ==="
for res in 1 2; do
    D="$(new_ws "bd${res}" "[workspace]
${MEMBERS}
resolver = \"${res}\"")"
    printf '\n[build-dependencies]\nshared = { path = "../shared", features = ["std"] }\n' >>"${D}/firmware/Cargo.toml"
    printf 'fn main() {\n    println!("cargo::rerun-if-changed=build.rs");\n    let _ = shared::checksum_hex(b"build");\n}\n' >"${D}/firmware/build.rs"
    echo "resolver ${res}:"
    fw_build "${D}" "  cargo build -p firmware" -p firmware
    shared_units "${D}" -p firmware
done

echo
echo "=== 7. Workspace inheritance: default-features = false on an inherited dependency ==="
for edition in 2021 2024; do
    D="$(new_ws "inh${edition}" "[workspace]
${MEMBERS}
resolver = \"2\"

[workspace.dependencies]
shared = { path = \"shared\" }")"
    cat >"${D}/firmware/Cargo.toml" <<EOF
[package]
name = "firmware"
version = "0.1.0"
edition = "${edition}"
publish = false

[dependencies]
shared = { workspace = true, default-features = false }

[lints.rust]
unexpected_cfgs = { level = "warn", check-cfg = ['cfg(firmware_target)'] }
EOF
    sed -i.bak '/^#\[cfg(test)\]/,$d' "${D}/firmware/src/lib.rs" && rm "${D}/firmware/src/lib.rs.bak"
    echo "firmware edition ${edition}:"
    printf '  shared features: %s\n' "$(shared_features "${D}" normal,build -p firmware)"
    fw_build "${D}" "  cargo build -p firmware" -p firmware
    # cargo build (not cargo tree) prints the manifest warning; keep only the reason.
    WARN="$(grep -m1 '`default-features` is ignored' <<<"${LAST_OUT}" | sed -E 's/^warning: [^:]*: //' || true)"
    echo "  cargo manifest warning: ${WARN:-none}"
done
D="$(new_ws inhfix "[workspace]
${MEMBERS}
resolver = \"2\"

[workspace.dependencies]
shared = { path = \"shared\", default-features = false }")"
sed -i.bak 's#^shared = { path = "../shared", default-features = false }#shared = { workspace = true }#; s#^shared = { path = "../shared", features = \["std"\] }#shared = { workspace = true, features = ["std"] }#' \
    "${D}/firmware/Cargo.toml" "${D}/host-tool/Cargo.toml"
rm "${D}"/firmware/Cargo.toml.bak "${D}"/host-tool/Cargo.toml.bak
echo "fix: workspace dependency declares default-features = false, members opt in to std:"
printf '  shared features for firmware: %s\n' "$(shared_features "${D}" normal,build -p firmware)"
printf '  shared features for host-tool: %s\n' "$(shared_features "${D}" normal,build -p host-tool)"
fw_build "${D}" "  cargo build -p firmware" -p firmware
