#!/bin/bash
set -euo pipefail

# Robust deployment script resolving quoting hazards,
# exit code masking, subshell scoping, and signal cleanup races.

STAGE_DIR="${STAGE_DIR:-./staging}"
RELEASE_DIR="${RELEASE_DIR:-./release}"
WORK_DIR="${WORK_DIR:-./tmp_workspace}"
CONFIG_FILE="${CONFIG_FILE:-./cluster.cfg}"

# Global process tracking and cleanup synchronization
DAEMON_PID=""
CLEANUP_INVOKED=0
ACTIVE_WORKSPACE=""
TERMINATING_SIGNAL=""

cleanup() {
    local exit_code=$?
    # Immediately de-arm traps to prevent re-entrant execution or trap race
    trap '' EXIT HUP INT TERM

    if [ "$CLEANUP_INVOKED" -eq 1 ]; then
        return
    fi
    CLEANUP_INVOKED=1

    # Terminate tracked background daemon if alive
    if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "[Robust Cleanup] Terminating background daemon..."
        kill -TERM "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi

    # Clean temporary scratch workspace
    if [ -n "$ACTIVE_WORKSPACE" ] && [ -d "$ACTIVE_WORKSPACE" ]; then
        rm -rf -- "$ACTIVE_WORKSPACE"
    fi

    # If terminated by a trapped signal, re-raise the signal to preserve exit status (128+N)
    if [ -n "$TERMINATING_SIGNAL" ]; then
        trap - "$TERMINATING_SIGNAL"
        kill -s "$TERMINATING_SIGNAL" "$$"
    fi

    exit "$exit_code"
}

handle_signal() {
    TERMINATING_SIGNAL="$1"
    cleanup
}

trap cleanup EXIT
trap 'handle_signal HUP' HUP
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM

init_config() {
    local config_path="$1"
    # Separate declaration and assignment to preserve command exit status
    local cluster_config
    cluster_config=$(cat "$config_path")
    echo "Loaded cluster config: [${cluster_config}]"
}

unpack_artifacts() {
    local src_dir="$1"
    local dest_dir="$2"
    mkdir -p "$dest_dir"

    # Use nullglob and array expansion with strict quoting to handle spaces and empty sets
    local -a pkgs
    shopt -s nullglob
    pkgs=("$src_dir"/*.tar.gz)
    shopt -u nullglob

    if [ "${#pkgs[@]}" -eq 0 ]; then
        echo "No artifacts found in $src_dir"
        return 0
    fi

    for pkg in "${pkgs[@]}"; do
        echo "Processing artifact: $pkg"
        tar -xzf "$pkg" -C "$dest_dir"
    done
}

count_deployed() {
    local target_dir="$1"
    local deployed_count=0

    # Process substitution: loop executes in parent shell, preserving variable modifications
    # while IFS= read -r -d '' ensures safe NUL-delimited stream parsing without backslash escaping
    while IFS= read -r -d '' _entry; do
        deployed_count=$((deployed_count + 1))
    done < <(find "$target_dir" -type f -name "*.txt" -print0)

    echo "Total deployed count: $deployed_count"
}

start_background_telemetry() {
    local workspace="$1"
    ACTIVE_WORKSPACE="$workspace"
    mkdir -p "$workspace"

    ./worker_daemon.sh "$workspace/daemon.log" &
    DAEMON_PID=$!
    echo "$DAEMON_PID" > "$workspace/daemon.pid"

    echo "Deploy script running with background daemon (PID: tracked)"
    wait "$DAEMON_PID"
}

case "${1:-all}" in
    test-sc2155)
        init_config "$2"
        ;;
    test-word-split)
        unpack_artifacts "$2" "$3"
        ;;
    test-subshell)
        count_deployed "$2"
        ;;
    test-trap-leak)
        start_background_telemetry "$2"
        ;;
    all)
        init_config "$CONFIG_FILE"
        unpack_artifacts "$STAGE_DIR" "$RELEASE_DIR"
        count_deployed "$RELEASE_DIR"
        start_background_telemetry "$WORK_DIR"
        ;;
    *)
        echo "Usage: $0 {test-sc2155|test-word-split|test-subshell|test-trap-leak|all}"
        exit 1
        ;;
esac
