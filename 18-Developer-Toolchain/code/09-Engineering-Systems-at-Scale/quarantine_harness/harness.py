#!/usr/bin/env python3
"""Quarantine lab: measure, classify, quarantine with a guard, then reproduce and verify a fix.

Part 1 is sample-size arithmetic.
Parts 2 to 4 replay a deterministic synthetic CI history (history.py) through the classifier and
three quarantine policies.
Parts 5 and 6 run REAL unittest code in subprocesses: an order-dependent test found by
shuffling and minimized with delta debugging, and a random-data test replayed by its seed.
"""
import difflib
import hashlib
import json
import math
import os
import subprocess
import sys
import unittest
from pathlib import Path

import flakelib as fl
import history as H

HERE = Path(__file__).resolve().parent
CHECKS = []


def check(name, ok):
    CHECKS.append((name, bool(ok)))
    print("check %-52s %s" % (name, "PASS" if ok else "FAIL"))


def heading(text):
    print()
    print("== %s ==" % text)


# ---------------------------------------------------------------- part 1
def part1():
    heading("Part 1: how many runs does a verdict need")
    print("runs n so that a flake of rate p fails at least once: n = ceil(ln(1-C) / ln(1-p))")
    print("  %-8s %8s %8s" % ("p", "C=95%", "C=99%"))
    for p in (0.30, 0.10, 0.01, 0.001):
        print("  %-8s %8d %8d" % (p, fl.runs_to_detect(p, 0.95), fl.runs_to_detect(p, 0.99)))
    print("zero failures in n runs certify rate < p0 at 95% only when n >= ln(0.05) / ln(1-p0)")
    print("  %-8s %8s %12s" % ("p0", "n exact", "3/p0 (rule of 3)"))
    for p0 in (0.05, 0.01, 0.001):
        print("  %-8s %8d %12d" % (p0, fl.runs_to_detect(p0, 0.95), math.ceil(3 / p0)))
    print("P(presubmit red from flakes alone) = 1 - (1-p)^N for N independent tests")
    print("  %-8s %-8s %10s" % ("N", "p", "P(red)"))
    for n_tests, p in ((1000, 0.001), (1000, 0.0001), (5000, 0.0001)):
        print("  %-8d %-8s %10.4f" % (n_tests, p, fl.p_any_flake(n_tests, p)))
    check("rule of 3 matches exact bound within 0.5% at p0=0.001",
          abs(fl.runs_to_detect(0.001, 0.95) - 3 / 0.001) / (3 / 0.001) < 0.005)


# ---------------------------------------------------------------- part 2
def part2(results):
    heading("Part 2: audit 40 commits x 2 runs x 120 tests")
    infra = fl.infra_runs(results, H.TESTS, H.N_COMMITS, H.N_RUNS)
    infra_commits = sorted({c for c, _ in infra})
    for c, r in sorted(infra):
        n = sum(1 for t in H.TESTS if results[(t, c, r)])
        print("infra event: commit c%02d run %d failed %d of %d tests (%.0f%%), excluded from evidence"
              % (c, r, n, len(H.TESTS), 100.0 * n / len(H.TESTS)))
    excluded = set(infra_commits)
    n_runs = (H.N_COMMITS - len(excluded)) * H.N_RUNS
    labels = {t: fl.classify(results, t, H.N_COMMITS, H.N_RUNS, excluded) for t in H.TESTS}
    order = {"DETERMINISTIC": 0, "SHIFT": 1, "FLAKY": 2, "WATCH": 3, "CLEAN": 4}
    rows = sorted((t for t in H.TESTS if labels[t]["label"] != "CLEAN"),
                  key=lambda t: (order[labels[t]["label"]], -labels[t]["fails"], t))
    print("%-22s %-14s %7s %7s %-15s %5s  %s"
          % ("test", "label", "fails", "rate", "wilson95", "mixed", "detail"))
    for t in rows:
        info = labels[t]
        lo, hi = fl.wilson(info["fails"], n_runs)
        detail = ""
        if info["windows"]:
            detail = "all runs failed c%02d..c%02d" % info["windows"][0]
        elif info["shift"]:
            cp = info["shift"]
            detail = "rate %.2f -> %.2f at c%02d, p_adj=%.1e" % (cp["rate_before"], cp["rate_after"],
                                                               cp["commit"], cp["p_adj"])
        print("%-22s %-14s %3d/%-3d %7.3f [%.3f,%.3f] %5d  %s"
              % (t, info["label"], info["fails"], n_runs, info["fails"] / n_runs, lo, hi,
                 len(info["mixed"]), detail))
    counts = {}
    for t in H.TESTS:
        counts[labels[t]["label"]] = counts.get(labels[t]["label"], 0) + 1
    print("label counts: " + ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts, key=order.get)))
    print("power: P(at least 1 failure in %d runs) at p=0.02 is %.2f, at p=0.005 is %.2f"
          % (n_runs, 1 - 0.98 ** n_runs, 1 - 0.995 ** n_runs))

    # negative control: the same classifier with the infra filter removed
    raw = {t: fl.classify(results, t, H.N_COMMITS, H.N_RUNS, set()) for t in H.TESTS}
    stable = [t for t in H.TESTS if t.startswith("stable_")]
    false_flaky = [t for t in stable if raw[t]["label"] in ("FLAKY", "WATCH", "SHIFT")]
    print("control: without the infra filter, %d of %d stable tests are flagged" % (len(false_flaky), len(stable)))

    check("filtered: no stable test is flagged",
          all(labels[t]["label"] == "CLEAN" for t in stable))
    check("unfiltered control flags stable tests (filter matters)", len(false_flaky) > 10)
    check("flaky_checkout labelled FLAKY", labels[H.FLAKY_CHECKOUT]["label"] == "FLAKY")
    check("regress_ledger labelled DETERMINISTIC c24..c28",
          labels[H.REGRESS_LEDGER]["label"] == "DETERMINISTIC"
          and labels[H.REGRESS_LEDGER]["windows"][0] == (24, 28))
    check("flaky_cache_ttl labelled DETERMINISTIC from c30",
          labels[H.FLAKY_CACHE_TTL]["label"] == "DETERMINISTIC"
          and labels[H.FLAKY_CACHE_TTL]["windows"][0] == (30, 39))
    shift = labels[H.SHIFT_ORDERS_DEDUP]
    check("shift_orders_dedup labelled SHIFT within c27..c30",
          shift["label"] == "SHIFT" and 27 <= shift["shift"]["commit"] <= 30)
    return labels, excluded


# ---------------------------------------------------------------- part 3
def part3(results, labels, excluded):
    heading("Part 3: replay the history through four policies")
    mixed = {t: fl.mixed_commits(results, t, H.N_COMMITS, H.N_RUNS, excluded) for t in H.TESTS}
    infra = fl.infra_runs(results, H.TESTS, H.N_COMMITS, H.N_RUNS)

    def eff_run(c):
        return 1 if (c, 0) in infra else 0

    def failed(t, c):
        return results[(t, c, eff_run(c))]

    def quarantine_start(t):
        seen = mixed[t]
        return seen[1] + 1 if len(seen) >= 2 else None   # state after the 2nd same-SHA mixed commit

    def is_quarantined(t, c):
        start = quarantine_start(t)
        return start is not None and c >= start

    def verdict(policy, t, c):
        """Returns (blocking, extra_executions, guard_fired)."""
        if not failed(t, c):
            return (False, 0, False)
        if policy == "none":
            return (True, 0, False)
        if policy == "retry3":
            extra = 0
            for rr in (2, 3):
                extra += 1
                if not H.fails(t, c, rr):
                    return (False, extra, False)
            return (True, extra, False)
        if not is_quarantined(t, c):
            return (True, 0, False)
        if policy == "naive":
            return (False, 0, False)
        extra = 0
        for rr in range(2, 7):
            extra += 1
            if not H.fails(t, c, rr):
                return (False, extra, False)
        return (True, extra, True)

    defects = (H.REGRESS_LEDGER, H.FLAKY_CACHE_TTL, H.SHIFT_ORDERS_DEDUP)
    names = (("none", "no quarantine"), ("retry3", "retry x3, all tests"),
             ("naive", "quarantine, ignore"), ("guarded", "quarantine + guard"))
    summary = {}
    guard_events = []
    print("swallowed = non-blocking failures; the defect columns count only commits where the test is really broken")
    print("%-22s %6s  %-31s %6s %6s" % ("policy", "red", "defect: ledger ttl dedup", "all", "extra"))
    for policy, title in names:
        red = 0
        swallowed = {t: 0 for t in defects}
        swallowed_all = 0
        extra_total = 0
        for c in range(H.N_COMMITS):
            is_red = False
            for t in H.TESTS:
                blocking, extra, fired = verdict(policy, t, c)
                extra_total += extra
                if fired and policy == "guarded":
                    guard_events.append((t, c))
                if blocking:
                    is_red = True
                elif failed(t, c):
                    swallowed_all += 1
                    if t in swallowed and H.fail_prob(t, c) >= 0.5:   # scored against ground truth
                        swallowed[t] += 1
            red += is_red
        summary[policy] = (red, swallowed, swallowed_all, extra_total)
        print("%-22s %3d/%-2d  %-31s %6d %6d"
              % (title, red, H.N_COMMITS,
                 "%d %d %d" % tuple(swallowed[t] for t in defects), swallowed_all, extra_total))
    print("guard fired (quarantined test failed all 5 reruns on the same SHA): %d times, first %s"
          % (len(guard_events), ", ".join("%s at c%02d" % e for e in guard_events[:1]) or "never"))

    check("guarded: fewer red commits than no quarantine", summary["guarded"][0] < summary["none"][0])
    check("guarded: regress_ledger never swallowed", summary["guarded"][1][H.REGRESS_LEDGER] == 0)
    check("naive hides the break landing on a quarantined test",
          summary["naive"][1][H.FLAKY_CACHE_TTL] >= 8)
    check("guarded blocks the same break at its first commit",
          summary["guarded"][1][H.FLAKY_CACHE_TTL] == 0
          and (H.FLAKY_CACHE_TTL, 30) in guard_events)
    check("guard is blind to a partial regression (dedup swallowed)",
          summary["guarded"][1][H.SHIFT_ORDERS_DEDUP] > 0)
    check("retry x3 swallows partial regression too, at extra runs",
          summary["retry3"][1][H.SHIFT_ORDERS_DEDUP] > 0 and summary["retry3"][3] > 0)

    heading("Part 4: tickets (as of %s)" % H.commit_date(H.N_COMMITS - 1))
    today = H.commit_date(H.N_COMMITS - 1)
    tickets = []
    for t in H.TESTS:
        start = quarantine_start(t)
        if start is None:
            continue
        info = labels[t]
        due = H.commit_date(start) + (H.commit_date(1) - H.commit_date(0)) * 14
        if info["label"] == "DETERMINISTIC":
            kind, owner, pri = "regression-on-quarantined", H.OWNERS.get(t, "unowned"), "P0"
        elif info["label"] == "SHIFT":
            kind, owner, pri = "regression-suspect", H.author_of(info["shift"]["commit"]), "P1"
        else:
            kind, owner, pri = "flake", H.OWNERS.get(t, "unowned"), "P2"
        status = "OVERDUE" if due < today else "open"
        tickets.append((t, owner))
        print("TICKET %-20s %-26s %-3s owner=%-14s quarantined=c%02d(%s) due=%s %s"
              % (t, kind, pri, owner, start, H.commit_date(start), due, status))
    quarantined_tests = [t for t in H.TESTS if quarantine_start(t) is not None]
    check("every quarantined test got a ticket with a real owner",
          [t for t, _ in tickets] == quarantined_tests and all(o != "unowned" for _, o in tickets))


# ---------------------------------------------------------------- parts 5 and 6
class Subproc:
    def __init__(self):
        self.runs = 0

    def run(self, dirname, cls, order, seeds=None):
        cmd = [sys.executable, str(HERE / "runner.py"), "--dir", str(HERE / dirname),
               "--module", "test_demo", "--cls", cls, "--order", ",".join(order)]
        if seeds:
            cmd += ["--seeds", seeds]
        env = dict(os.environ)
        env.pop("TEST_SEED", None)
        out = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True).stdout
        self.runs += 1
        return json.loads(out.strip().splitlines()[-1])


def shuffled(names, seed):
    return sorted(names, key=lambda n: hashlib.sha256(("%d:%s" % (seed, n)).encode("ascii")).hexdigest())


def ddmin(items, fails):
    """Zeller's delta debugging: smallest sublist (order kept) that still fails."""
    n = 2
    while len(items) >= 2:
        size = math.ceil(len(items) / n)
        parts = [items[i:i + size] for i in range(0, len(items), size)]
        reduced = False
        for part in parts:
            if fails(part):
                items, n, reduced = part, 2, True
                break
        if not reduced:
            for i in range(len(parts)):
                comp = [x for j, p in enumerate(parts) if j != i for x in p]
                if fails(comp):
                    items, n, reduced = comp, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(items):
                break
            n = min(len(items), 2 * n)
    return items


def part5():
    heading("Part 5: order dependence on real unittest runs")
    sys.path.insert(0, str(HERE / "suite_bad"))
    import test_demo as bad   # imported only to list names; the tests run in subprocesses
    names = unittest.TestLoader().getTestCaseNames(bad.RegistryTests)
    sp = Subproc()
    base = sp.run("suite_bad", "RegistryTests", names)["failed"]
    print("default order (alphabetical), %d tests: %d failures" % (len(names), len(base)))

    seeds = list(range(1, 21))
    failing = [s for s in seeds if sp.run("suite_bad", "RegistryTests", shuffled(names, s))["failed"]]
    print("shuffled orders, seeds 1..20: %d fail (expect about 10: P(polluter before victim) = 1/2)"
          % len(failing))
    first = failing[0]
    order = shuffled(names, first)
    victim = "test_a03_registry_starts_empty"
    got = sp.run("suite_bad", "RegistryTests", order)["failed"]
    print("first failing seed %d: failed=%s, victim at position %d of %d"
          % (first, got, order.index(victim) + 1, len(order)))

    alone = sp.run("suite_bad", "RegistryTests", [victim])["failed"]
    before = order[:order.index(victim)]
    count0 = sp.runs
    cache = {}

    def fails(subset):
        key = tuple(subset)
        if key not in cache:
            cache[key] = victim in sp.run("suite_bad", "RegistryTests", list(subset) + [victim])["failed"]
        return cache[key]

    minimal = ddmin(before, fails)
    print("ddmin: %d tests before the victim reduced to %d in %d subprocess runs"
          % (len(before), len(minimal), sp.runs - count0))
    print("minimal reproducer: %s" % ", ".join(minimal + [victim]))

    fixed_same = sp.run("suite_fixed", "RegistryTests", order)["failed"]
    fixed_fail = [s for s in seeds if sp.run("suite_fixed", "RegistryTests", shuffled(names, s))["failed"]]
    diff = [ln for ln in difflib.unified_diff(
        (HERE / "suite_bad" / "test_demo.py").read_text().splitlines(),
        (HERE / "suite_fixed" / "test_demo.py").read_text().splitlines(), lineterm="", n=0)
        if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    print("fix touches %d diff lines in the module; same failing order on fixed suite: %d failures"
          % (len(diff), len(fixed_same)))
    print("fixed suite over shuffled seeds 1..20: %d failures" % len(fixed_fail))

    check("default order passes (the flake hides in CI)", len(base) == 0)
    check("shuffling exposes the order dependence", 0 < len(failing) < len(seeds))
    check("victim alone passes (it is a victim, not broken)", alone == [])
    check("ddmin isolates exactly the polluter",
          minimal == ["test_a09_registers_plugin"])
    check("the same failing order passes after the fix", fixed_same == [])
    check("fixed suite passes every shuffle", fixed_fail == [])


def part6():
    heading("Part 6: a random-data flake replayed by its seed")
    sp = Subproc()
    all_seeds = sp.run("suite_bad", "IdTests", ["test_ids_unique"], seeds="0-199")["failed_seeds"]
    exact = 1 - (16 * 15 * 14 * 13) / 16 ** 4
    print("4 draws from 16 values collide with probability 1 - 16*15*14*13/16^4 = %.4f" % exact)
    print("seeds 0..199: %d failures (expected %.1f)" % (len(all_seeds), 200 * exact))
    bad_seed = all_seeds[0]
    good_seed = next(s for s in range(200) if s not in all_seeds)
    replay_bad = [sp.run("suite_bad", "IdTests", ["test_ids_unique"], seeds="%d-%d" % (bad_seed, bad_seed))
                  ["failed_seeds"] for _ in range(5)]
    replay_good = [sp.run("suite_bad", "IdTests", ["test_ids_unique"], seeds="%d-%d" % (good_seed, good_seed))
                   ["failed_seeds"] for _ in range(5)]
    print("replay seed %d five times: %d of 5 fail" % (bad_seed, sum(1 for r in replay_bad if r)))
    print("replay seed %d five times: %d of 5 fail" % (good_seed, sum(1 for r in replay_good if r)))
    print("runs needed to see this flake once at 95%%: %d" % fl.runs_to_detect(exact, 0.95))
    fixed = sp.run("suite_fixed", "IdTests", ["test_ids_unique"], seeds="0-199")["failed_seeds"]
    bound = fl.zero_failure_upper(200)
    print("fixed suite, the same 200 seeds: %d failures" % len(fixed))
    print("0 failures in 200 runs: rate < %.4f at 95%% (exact), so it certifies p0=0.05 but not 0.01 (needs %d)"
          % (bound, fl.runs_to_detect(0.01, 0.95)))

    check("observed failure count within 25% of binomial mean",
          abs(len(all_seeds) - 200 * exact) / (200 * exact) < 0.25)
    check("a failing seed fails on every replay", all(replay_bad))
    check("a passing seed passes on every replay", not any(replay_good))
    check("fixed suite passes the same 200 seeds", fixed == [])
    check("200 clean runs cannot certify 1% (bound above 0.01)", bound > 0.01)


def main():
    part1()
    results = H.build()
    labels, excluded = part2(results)
    part3(results, labels, excluded)
    part5()
    part6()
    heading("Checks")
    failed = [name for name, ok in CHECKS if not ok]
    print("%d of %d checks passed" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print("FAILED: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
