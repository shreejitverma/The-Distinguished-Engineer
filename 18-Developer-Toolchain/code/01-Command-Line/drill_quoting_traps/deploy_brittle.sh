#!/bin/bash
set -e

# Brittle deployment script demonstrating quoting hazards,
# command substitution masking, subshell loss, and trap races.

STAGE_DIR="${STAGE_DIR:-./staging}"
RELEASE_DIR="${RELEASE_DIR:-./release}"
WORK_DIR="${WORK_DIR:-./tmp_workspace}"
CONFIG_FILE="${CONFIG_FILE:-./cluster.cfg}"

init_config() {
    local config_path="$1"
    # SC2155: Combining local declaration with command substitution masks exit status
    local cluster_config=$(cat "$config_path")
    echo "Loaded cluster config: [${cluster_config}]"
}

unpack_artifacts() {
    local src_dir="$1"
    local dest_dir="$2"
    mkdir -p "$dest_dir"

    # SC2045 & SC2086: Unquoted command substitution over ls causes word splitting on spaces
    for pkg in $(ls $src_dir/*.tar.gz); do
        echo "Processing artifact: $pkg"
        tar -xzf $pkg -C "$dest_dir" >/dev/null 2>&1 || echo "ERROR: failed to extract $pkg"
    done
}

count_deployed() {
    local target_dir="$1"
    DEPLOYED_COUNT=0

    # Pipeline subshell: while loop runs in subshell; variable modification is lost
    # SC2162: read without -r mangles backslashes
    find "$target_dir" -type f -name "*.txt" | while read line; do
        DEPLOYED_COUNT=$((DEPLOYED_COUNT + 1))
    done

    echo "Total deployed count: $DEPLOYED_COUNT"
}

start_background_telemetry() {
    local workspace="$1"
    mkdir -p "$workspace"

    ./worker_daemon.sh "$workspace/daemon.log" &
    DAEMON_PID=$!
    echo "$DAEMON_PID" > "$workspace/daemon.pid"

    # Naive signal trap: does not signal child process, removes workspace while daemon runs,
    # and exits 1 instead of propagating the termination signal.
    trap 'echo "[Brittle Trap] Cleaning workspace without terminating background daemon"; rm -rf "$workspace"; exit 1' INT TERM

    echo "Deploy script running with background daemon (PID: $DAEMON_PID)"
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
