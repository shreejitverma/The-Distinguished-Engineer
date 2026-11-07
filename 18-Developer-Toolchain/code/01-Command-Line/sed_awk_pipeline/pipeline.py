#!/usr/bin/env python3
"""Synthetic log generator and reference validator for sed/awk stream processing lab."""

import os
import signal
import subprocess
import sys
from pathlib import Path

def generate_data(workdir: Path):
    # 1. Transaction stream for hold-space filtering
    tx_lines = [
        "TX_START tx-101",
        "service=auth",
        "method=POST path=/login",
        "latency_ms=15",
        "STATUS 200",
        "TX_END tx-101",
        "TX_START tx-102",
        "service=checkout",
        "method=POST path=/pay",
        "latency_ms=450",
        "STATUS 500",
        "error=db_deadlock",
        "TX_END tx-102",
        "TX_START tx-103",
        "service=search",
        "method=GET path=/query",
        "latency_ms=22",
        "STATUS 200",
        "TX_END tx-103",
        "TX_START tx-104",
        "service=orders",
        "method=GET path=/history",
        "latency_ms=890",
        "STATUS 503",
        "error=upstream_timeout",
        "TX_END tx-104",
        "TX_START tx-105",
        "service=inventory",
        "method=GET path=/items",
        "latency_ms=30",
        "STATUS 200",
        "TX_END tx-105",
    ]
    (workdir / "input_transactions.log").write_text("\n".join(tx_lines) + "\n", encoding="ascii")

    # 2. Asynchronous events for sliding window N;D;P pairing
    event_lines = [
        "REQ id=101 path=/auth",
        "RES id=101 status=200",
        "REQ id=102 path=/catalog",
        "REQ id=103 path=/search",
        "RES id=103 status=500",
        "REQ id=104 path=/cart",
        "RES id=104 status=200",
        "REQ id=105 path=/checkout",
    ]
    (workdir / "input_events.log").write_text("\n".join(event_lines) + "\n", encoding="ascii")

    # 3. Nested bracket expressions for branching loop
    nested_lines = [
        "meta=[k1=[k2=[k3=val1]]]",
        "tags=[env=[tier=[cluster=prod]]]",
        "flags=[debug=[trace=[level=max]]]",
    ]
    (workdir / "input_nested.log").write_text("\n".join(nested_lines) + "\n", encoding="ascii")

    # 4. Multi-line record log for awk RS/FS parsing
    record_blocks = [
        "service=auth\nregion=us-east\nstatus=200\nlatency_ms=14\n---\n",
        "service=auth\nregion=us-east\nstatus=500\nlatency_ms=120\n---\n",
        "service=auth\nregion=eu-west\nstatus=200\nlatency_ms=700\n---\n",
        "service=checkout\nregion=us-east\nstatus=200\nlatency_ms=45\n---\n",
        "service=checkout\nregion=us-east\nstatus=500\nlatency_ms=510\n---\n",
        "service=checkout\nregion=eu-west\nstatus=200\nlatency_ms=62\n---\n",
        "service=search api\nregion=us-east\nstatus=200\nlatency_ms=25\n---\n",
        "service=search api\nregion=eu-west\nstatus=200\nlatency_ms=30\n---\n",
    ]
    (workdir / "input_records.log").write_text("".join(record_blocks), encoding="ascii")


def verify(workdir: Path):
    print("Verification stages:")
    # Verify 1: Hold space transaction filter
    tx_out = (workdir / "out_tx.log").read_text(encoding="ascii").splitlines()
    expected_tx = [
        "TX_START tx-102",
        "service=checkout",
        "method=POST path=/pay",
        "latency_ms=450",
        "STATUS 500",
        "error=db_deadlock",
        "TX_END tx-102",
        "TX_START tx-104",
        "service=orders",
        "method=GET path=/history",
        "latency_ms=890",
        "STATUS 503",
        "error=upstream_timeout",
        "TX_END tx-104",
    ]
    assert tx_out == expected_tx, f"transactions mismatch: got {tx_out}, expected {expected_tx}"
    print("  Stage 1 (sed hold-space block filter): PASS (2 failed transactions isolated)")

    # Verify 2: Sliding window N;D;P correlator
    ev_out = (workdir / "out_events.log").read_text(encoding="ascii").strip().splitlines()
    expected_ev = [
        "PAIR id=101 path=/auth status=200",
        "REQ id=102 path=/catalog",
        "PAIR id=103 path=/search status=500",
        "PAIR id=104 path=/cart status=200",
        "REQ id=105 path=/checkout",
    ]
    assert ev_out == expected_ev, f"events mismatch: got {ev_out}, expected {expected_ev}"
    print("  Stage 2 (sed N;D;P sliding-window pair): PASS (3 pairs merged, 2 orphans kept)")

    # Verify 3: Branching loop unwrap
    nest_out = (workdir / "out_nested.log").read_text(encoding="ascii").strip().splitlines()
    expected_nest = [
        "meta=<k1=<k2=<k3=val1>>>",
        "tags=<env=<tier=<cluster=prod>>>",
        "flags=<debug=<trace=<level=max>>>",
    ]
    assert nest_out == expected_nest, f"nested mismatch: got {nest_out}, expected {expected_nest}"
    print("  Stage 3 (sed :loop t branching unwrap): PASS (3 lines fully normalized)")

    # Verify 4: Awk record and field aggregation
    awk_out = (workdir / "out_awk_metrics.log").read_text(encoding="ascii").strip().splitlines()
    awk_out.sort()
    expected_awk = [
        "auth|eu-west|1|0|700.0",
        "auth|us-east|2|1|67.0",
        "checkout|eu-west|1|0|62.0",
        "checkout|us-east|2|1|277.5",
        "search api|eu-west|1|0|30.0",
        "search api|us-east|1|0|25.0",
    ]
    assert awk_out == expected_awk, f"awk metrics mismatch: got {awk_out}, expected {expected_awk}"
    print("  Stage 4 (awk RS/FS composite aggregation): PASS (6 composite keys aggregated)")

    # Verify 5: SUBSEP key inspection (byte value is measured by run.sh, not assumed)
    subsep_out = (workdir / "out_subsep.log").read_text(encoding="ascii").strip().splitlines()
    assert subsep_out == ["key_len=11 subsep_len=1 subsep_idx=5 subsep_hex=1c"], f"subsep mismatch: {subsep_out}"
    print("  Stage 5 (awk SUBSEP byte layout): PASS (file separator 0x1C measured)")

    # Verify 6: Gawk coprocess two-way sort; the output file is mandatory, a missing one is a failure
    coproc_out = (workdir / "out_coproc.log").read_text(encoding="ascii").strip().splitlines()
    expected_coproc = [
        "checkout|us-east|2|1|277.5",
        "auth|us-east|2|1|67.0",
        "auth|eu-west|1|0|700.0",
        "checkout|eu-west|1|0|62.0",
        "search api|eu-west|1|0|30.0",
        "search api|us-east|1|0|25.0",
    ]
    assert coproc_out == expected_coproc, f"coproc mismatch: got {coproc_out}, expected {expected_coproc}"
    print("  Stage 6 (gawk |& coprocess two-way pipe): PASS (half-duplex close and sorted)")

    # Verify 6b: without close(cmd, "to") the same program must not finish
    state = (workdir / "out_nohalfclose.log").read_text(encoding="ascii").strip()
    assert state == "blocked", f"expected the no-close variant to block, got {state!r}"
    print("  Stage 6b (gawk without close(cmd, \"to\")): PASS (blocked until the 3 s deadline)")

    print("Stream processing pipeline verification: ALL CHECKS PASSED")


def deadline(seconds: float, out_file: Path, cmd: list[str]):
    """Run cmd with a deadline; write 'finished' or 'blocked' to out_file and kill the whole process group."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        proc.wait(timeout=seconds)
        state = "finished"
    except subprocess.TimeoutExpired:
        state = "blocked"
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    out_file.write_text(state + "\n", encoding="ascii")


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "deadline" and sys.argv[4] == "--":
        deadline(float(sys.argv[2]), Path(sys.argv[3]), sys.argv[5:])
        sys.exit(0)
    if len(sys.argv) < 3:
        print("Usage: pipeline.py [generate|verify] <workdir> | deadline <seconds> <out_file> -- <cmd...>")
        sys.exit(1)
    action, target_dir = sys.argv[1], Path(sys.argv[2])
    if action == "generate":
        generate_data(target_dir)
    elif action == "verify":
        verify(target_dir)
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)
