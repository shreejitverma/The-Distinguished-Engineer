#!/usr/bin/env python3
"""Validates the folded stacks emitted by the capstone sampling profiler.

Checks (any failure prints FAIL lines and exits 1):
1. Every input line is `frame;frame;... count` with a positive integer count.
2. More than MIN_SAMPLES samples were collected.
3. All three workload tasks (task_alpha, task_beta, task_gamma) and leaf_worker are captured.
4. Stack order is root-to-leaf: leaf_worker is the last frame of every stack that has it,
   and a task_* frame always precedes it.
5. No frame is an AArch64 mapping symbol (a name starting with `$`).
6. Proportions roughly conform to 5:3:2 (alpha ~ 50%, beta ~ 30%, gamma ~ 20%).

On success the stdout summary is deterministic, because it prints only booleans.
"""
import sys

# Roughly 500 samples at 1000 Hz for 240 rounds; a HZ=250 kernel still yields about 125.
MIN_SAMPLES = 50
TASKS = ("task_alpha", "task_beta", "task_gamma")


def main():
    counts = dict.fromkeys(TASKS, 0)
    total_samples = 0
    leaf_worker_found = False
    order_ok = True
    mapping_symbols = 0
    errors = []

    for lineno, raw in enumerate(sys.stdin, 1):
        line = raw.strip()
        if not line:
            continue
        stack, _, count_str = line.rpartition(" ")
        if not stack or not count_str.isdigit() or int(count_str) <= 0:
            errors.append(f"line {lineno}: not in 'stack count' form: {line[:80]!r}")
            continue
        count = int(count_str)
        frames = stack.split(";")
        total_samples += count

        if any(f.startswith("$") for f in frames):
            mapping_symbols += 1

        # The workload is: main -> run_workload_round -> task_* -> leaf_worker.
        # A sample taken inside leaf_worker must have it as the last frame (it calls nothing),
        # with its task before it. A reversed (leaf-to-root) emitter fails this.
        leaf_idx = [i for i, f in enumerate(frames) if "leaf_worker" in f]
        if leaf_idx:
            leaf_worker_found = True
            task_idx = [i for i, f in enumerate(frames) if any(t in f for t in TASKS)]
            if leaf_idx[-1] != len(frames) - 1 or not task_idx or task_idx[0] >= leaf_idx[0]:
                order_ok = False

        for task in TASKS:
            if any(task in f for f in frames):
                counts[task] += count
                break

    workload_samples = sum(counts.values())
    if workload_samples == 0:
        print("FAIL: No workload samples captured")
        sys.exit(1)

    pct = {t: counts[t] * 100.0 / workload_samples for t in TASKS}
    alpha, beta, gamma = (counts[t] for t in TASKS)

    checks = [
        ("Total samples collected", total_samples > MIN_SAMPLES),
        ("Leaf worker resolved", leaf_worker_found),
        ("Stack order verified: root-to-leaf", order_ok),
        ("Task alpha (expected ~50%)", 40.0 <= pct["task_alpha"] <= 60.0),
        ("Task beta (expected ~30%)", 20.0 <= pct["task_beta"] <= 40.0),
        ("Task gamma (expected ~20%)", 10.0 <= pct["task_gamma"] <= 30.0),
        ("Proportions monotonic (alpha > beta > gamma)", alpha > beta > gamma),
    ]
    passed = not errors and mapping_symbols == 0

    for name, ok in checks:
        passed = passed and ok
        # The order line prints its fixed label on success so the recorded summary is stable.
        if name.startswith("Stack order verified") and ok:
            print(name)
        else:
            print(f"{name}: {ok}")

    for err in errors:
        print(f"FAIL: {err}")
    if mapping_symbols:
        print(f"FAIL: {mapping_symbols} stack(s) contain a '$' mapping symbol frame")

    if not passed:
        print("Capstone sampling profiler verification: FAIL")
        sys.exit(1)
    print("Capstone sampling profiler verification: PASS")


if __name__ == "__main__":
    main()
