#!/usr/bin/env python3
import os
import sys
import time

def main():
    if len(sys.argv) < 2:
        print("Usage: bench.py <host_mount_dir>", file=sys.stderr)
        sys.exit(1)

    host_mount = sys.argv[1]

    print("=== macOS Container & VM Hypervisor I/O Benchmark ===")
    uname = os.uname()
    print(f"Guest kernel: {uname.sysname} {uname.machine}")

    # 1. Mount Inspection
    root_fs = "unknown"
    host_fs = "unknown"
    with open("/proc/mounts", "r") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3:
                if parts[1] == "/":
                    root_fs = parts[2]
                elif parts[1] == host_mount:
                    host_fs = parts[2]

    print(f"Guest root filesystem: {root_fs}")
    print(f"Host bind mount filesystem: {host_fs}")

    # 2. Filesystem Semantics (Case Sensitivity)
    case_nat_dir = "/tmp/bench_case_test"
    case_host_dir = os.path.join(host_mount, "bench_case_test")
    os.makedirs(case_nat_dir, exist_ok=True)
    os.makedirs(case_host_dir, exist_ok=True)

    with open(os.path.join(case_nat_dir, "probe.txt"), "w") as f:
        f.write("a")
    with open(os.path.join(case_nat_dir, "PROBE.TXT"), "w") as f:
        f.write("b")
    nat_distinct = len([f for f in os.listdir(case_nat_dir) if f.lower() == "probe.txt"])

    with open(os.path.join(case_host_dir, "probe.txt"), "w") as f:
        f.write("a")
    with open(os.path.join(case_host_dir, "PROBE.TXT"), "w") as f:
        f.write("b")
    host_distinct = len([f for f in os.listdir(case_host_dir) if f.lower() == "probe.txt"])

    print(f"Native overlayfs case-sensitive: {nat_distinct == 2} ({nat_distinct} entries)")
    print(f"Host bind mount case-sensitive: {host_distinct == 2} ({host_distinct} entry, APFS case folding)")

    # 3. Metadata I/O Benchmark
    def run_cycle(base_dir, n=500):
        os.makedirs(base_dir, exist_ok=True)
        t0 = time.perf_counter()
        for i in range(n):
            with open(os.path.join(base_dir, f"file_{i:04d}.dat"), "w") as f:
                f.write(f"sample payload line {i:04d}\n")
        t1 = time.perf_counter()
        for i in range(n):
            os.stat(os.path.join(base_dir, f"file_{i:04d}.dat"))
        t2 = time.perf_counter()
        entries = os.listdir(base_dir)
        t3 = time.perf_counter()
        for i in range(n):
            os.unlink(os.path.join(base_dir, f"file_{i:04d}.dat"))
        t4 = time.perf_counter()
        return (t1 - t0) + (t2 - t1) + (t3 - t2) + (t4 - t3), len(entries)

    dur_nat, count_nat = run_cycle("/tmp/bench_native_io", 500)
    dur_host, count_host = run_cycle(os.path.join(host_mount, "bench_host_io"), 500)

    print(f"File count verified: native={count_nat}/500, host={count_host}/500")
    if dur_host > dur_nat:
        print("Metadata latency check: PASS (host bind mount > native overlayfs)")
    else:
        print("Metadata latency check: FAIL (unexpected overhead relationship)")

    # 4. Cross-Boundary Synchronization Token
    token_path = os.path.join(host_mount, "sync_token.txt")
    with open(token_path, "w") as f:
        f.write("vm_write_ok\n")
    print("Guest sync token written: OK")

if __name__ == "__main__":
    main()
