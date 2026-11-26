"""Flaky-test and build-latency economics, simulated with seeded randomness.

Every model parameter below is an assumption chosen for illustration, not a measurement of
any real system. The lab checks mechanisms (how probabilities compound, how a policy shifts
a cost), and each assert can fail if the mechanism in question were implemented wrongly.
"""

import math
import random


def p_any_flake(f, n):
    """P(at least one of n independent runs flakes) when each flakes with probability f."""
    return 1.0 - (1.0 - f) ** n


# ---------------------------------------------------------------- A: flakes compound
def section_a():
    print("A. Spurious-red probability of one presubmit: 1 - (1 - f)^T")
    print("   f = per-test-run flake rate, T = tests executed")
    print("   %-8s %10s %10s %10s" % ("f", "T=100", "T=1000", "T=10000"))
    for f in (0.0001, 0.001, 0.0015, 0.01):
        print("   %-8s %10.4f %10.4f %10.4f" % (f, p_any_flake(f, 100), p_any_flake(f, 1000), p_any_flake(f, 10000)))

    rng = random.Random(20261121)
    f, t, trials = 0.005, 200, 4000
    red = sum(1 for _ in range(trials) if any(rng.random() < f for _ in range(t)))
    closed = p_any_flake(f, t)
    print("   Monte Carlo check, f=%s T=%d, %d trials: closed form %.4f, simulated %.4f" % (f, t, trials, closed, red / trials))
    assert abs(closed - red / trials) < 0.02, "Monte Carlo disagrees with the closed form"

    n_pool, per_change, f = 3000, 300, 0.0002
    print()
    print("   Batch growth model: pool N=%d tests, each change selects %d random tests, f=%s" % (n_pool, per_change, f))
    print("   %-6s %-14s %-12s" % ("B", "tests run U(B)", "P(spurious red)"))
    prev_u = prev_p = 0.0
    for b in (1, 2, 4, 8, 16, 32):
        u = n_pool * (1.0 - (1.0 - per_change / n_pool) ** b)
        print("   %-6d %-14.0f %-12.4f" % (b, u, p_any_flake(f, u)))
        assert u > prev_u and p_any_flake(f, u) > prev_p, "bigger batches must run more tests and flake more"
        assert u < per_change * b or b == 1, "union of overlapping test sets must grow sublinearly"
        prev_u, prev_p = u, p_any_flake(f, u)
    assert round(n_pool * (1.0 - (1.0 - per_change / n_pool) ** 32)) == 2897


# ---------------------------------------------------------------- B: retries
def section_b():
    f, tests = 0.01, 1000
    print("B. Retry economics: f=%s per test run, T=%d tests, every test takes one time unit" % (f, tests))
    print("   rounds = expected sequential attempt rounds on the critical path")
    print("   %-9s %-18s %-24s %-18s %-8s" % ("attempts", "P(test false red)", "P(suite false red)", "extra runs/test", "rounds"))
    for k in (1, 2, 3):
        per_test = f ** k
        suite = p_any_flake(per_test, tests)
        extra = sum(f ** i for i in range(1, k))
        rounds = sum(1.0 - (1.0 - f ** j) ** tests for j in range(k))
        print("   %-9d %-18.6f %-24.6f %-18.4f %-8.4f" % (k, per_test, suite, extra, rounds))

    rng = random.Random(7)
    k, trials = 2, 3000
    rounds_sim = 0
    for _ in range(trials):
        attempt, pending = 0, tests
        while pending and attempt < k:
            attempt += 1
            pending = sum(1 for _ in range(pending) if rng.random() < f)
        rounds_sim += attempt
    rounds_closed = sum(1.0 - (1.0 - f ** j) ** tests for j in range(k))
    print("   Monte Carlo check, attempts=2: closed %.4f rounds, simulated %.4f rounds" % (rounds_closed, rounds_sim / trials))
    assert abs(rounds_closed - rounds_sim / trials) < 0.02


# ---------------------------------------------------------------- C: masking
def section_c():
    print("C. Masking: a real race regression fails a test with probability q per run")
    print("   %-6s %-22s %-22s %-24s" % ("q", "P(red) at 1 attempt", "P(red) at 3 attempts", "presubmits to first red (3)"))
    for q in (1.0, 0.5, 0.2):
        print("   %-6s %-22.4f %-22.4f %-24.1f" % (q, q, q ** 3, 1.0 / q ** 3))
    # Simulate the retry policy itself (not just the closed form): a presubmit is red only if
    # every one of its attempts fails, so retries must suppress detection of a real race.
    crng = random.Random(4242)
    trials = 20000
    for q in (0.5, 0.2):
        red1 = sum(1 for _ in range(trials) if crng.random() < q)
        red3 = sum(1 for _ in range(trials) if all(crng.random() < q for _ in range(3)))
        assert abs(red1 / trials - q) < 0.01, "single attempt must detect at rate q"
        assert abs(red3 / trials - q ** 3) < 0.01, "three attempts must detect only at rate q^3"
        assert red3 < red1, "retries must mask a real intermittent failure"


# ---------------------------------------------------------------- D: merge queue
def flake_prob_for_batch(b, n_pool=3000, per_change=300, f=0.0002):
    u = n_pool * (1.0 - (1.0 - per_change / n_pool) ** b)
    return p_any_flake(f, u)


class QueueSim:
    def __init__(self, seed, bad_rate, flaky, attempts):
        self.rng = random.Random(seed)
        self.bad_rate = bad_rate
        self.flaky = flaky
        self.attempts = attempts
        self.builds = 0
        self.false_ejects = 0
        self.true_ejects = 0
        self.merged = 0

    def build_is_red(self, changes):
        """Red only if a bad change is inside, or every attempt flaked."""
        if any(changes):
            self.builds += 1
            return True
        flake = flake_prob_for_batch(len(changes)) if self.flaky else 0.0
        for _ in range(self.attempts):
            self.builds += 1
            if self.rng.random() >= flake:
                return False
        return True

    def process(self, changes):
        if not self.build_is_red(changes):
            self.merged += len(changes)
        elif len(changes) == 1:
            if changes[0]:
                self.true_ejects += 1
            else:
                self.false_ejects += 1
        else:
            mid = len(changes) // 2
            self.process(changes[:mid])
            self.process(changes[mid:])

    def run(self, batch, total):
        for _ in range(total // batch):
            self.process([self.rng.random() < self.bad_rate for _ in range(batch)])


def section_d():
    total, bad = 19200, 0.03
    print("D. Merge-queue bisection: %d changes, %.0f%% truly bad, batch builds flake via model A" % (total, bad * 100))
    print("   Red batch is split in half and rebuilt; a red single change is ejected.")
    print("   builds/chg = builds per change; fe = false ejections per 1000 changes (good change ejected)")
    print("   %-4s %-14s %-12s %-8s %-12s %-8s" % ("B", "no flakes", "flaky,k=1", "fe", "flaky,k=2", "fe"))
    best = {}
    for b in (1, 2, 4, 8, 16, 32):
        row = []
        for flaky, k in ((False, 1), (True, 1), (True, 2)):
            sim = QueueSim(2026, bad, flaky, k)
            sim.run(b, total)
            row.append((sim.builds / total, 1000.0 * sim.false_ejects / total))
        print("   %-4d %-14.3f %-12.3f %-8.1f %-12.3f %-8.1f" % (b, row[0][0], row[1][0], row[1][1], row[2][0], row[2][1]))
        for name, value in zip(("none", "k1", "k2"), (row[0][0], row[1][0], row[2][0])):
            if name not in best or value < best[name][1]:
                best[name] = (b, value)
    print("   cheapest batch size: no flakes B=%d, flaky k=1 B=%d, flaky k=2 B=%d" % (best["none"][0], best["k1"][0], best["k2"][0]))
    assert best["k1"][0] <= best["none"][0], "flakiness must not push the optimum batch size up"


# ---------------------------------------------------------------- E: detection
def section_e():
    rng = random.Random(424242)
    n_tests, commits, extra_attempts, broken_from = 600, 100, 2, 60
    tests = []
    for i in range(n_tests):
        x = rng.random()
        if i < 5:
            tests.append(("broken", 0.0))
        elif x < 0.80:
            tests.append(("stable", 0.0))
        elif x < 0.93:
            tests.append(("flaky", 0.002 + 0.018 * rng.random()))
        elif x < 0.98:
            tests.append(("flaky", 0.02 + 0.08 * rng.random()))
        else:
            tests.append(("flaky", 0.1 + 0.4 * rng.random()))

    def run_commit(kind, pf, commit):
        """First attempt, then reruns while red. Returns the list of attempt results (True = fail)."""
        def one():
            if kind == "broken" and commit >= broken_from:
                return True
            return rng.random() < pf
        results = [one()]
        while results[-1] and len(results) < 1 + extra_attempts:
            results.append(one())
        return results

    buckets = [("0.002-0.02", 0.002, 0.02), ("0.02-0.10", 0.02, 0.10), ("0.10-0.50", 0.10, 0.50)]
    seen = {name: [0, 0, 0.0] for name, _, _ in buckets}
    flagged_stable = flagged_broken = 0
    broken_all_fail_tail = 0
    for kind, pf in tests:
        mixed = False
        tail_all_fail = True
        for commit in range(commits):
            res = run_commit(kind, pf, commit)
            if True in res and False in res:
                mixed = True
            if commit >= commits - 3 and not all(res):
                tail_all_fail = False
        if kind == "stable" and mixed:
            flagged_stable += 1
        if kind == "broken":
            flagged_broken += 1 if mixed else 0
            broken_all_fail_tail += 1 if tail_all_fail and all(res) else 0
        if kind == "flaky":
            for name, lo, hi in buckets:
                if lo <= pf < hi:
                    seen[name][0] += 1
                    seen[name][1] += 1 if mixed else 0
                    seen[name][2] += 1.0 - (1.0 - pf * (1.0 - pf ** extra_attempts)) ** commits
    print("E. Rerun detection: %d tests, %d commits, up to %d reruns of a red first attempt" % (n_tests, commits, extra_attempts))
    print("   Flag = some commit shows both a fail and a pass on the same code (same-commit evidence).")
    print("   %-12s %-6s %-9s %-10s" % ("true pf", "tests", "flagged", "expected"))
    for name, _, _ in buckets:
        n, hit, expect = seen[name]
        print("   %-12s %-6d %-9d %-10.1f" % (name, n, hit, expect))
    print("   stable tests flagged flaky: %d of %d" % (flagged_stable, sum(1 for k, _ in tests if k == "stable")))
    print("   broken tests (fail every attempt from commit %d) flagged flaky: %d of 5, red on all attempts at the tail: %d of 5" % (broken_from, flagged_broken, broken_all_fail_tail))
    assert flagged_stable == 0 and flagged_broken == 0, "same-commit evidence must never flag deterministic tests"
    assert broken_all_fail_tail == 5

    p0, p1, alpha, beta = 0.005, 0.05, 0.01, 0.01
    upper = math.log((1 - beta) / alpha)
    lower = math.log(beta / (1 - alpha))
    llr_fail = math.log(p1 / p0)
    llr_pass = math.log((1 - p1) / (1 - p0))
    print()
    print("   SPRT quarantine rule on stress runs of a known-good commit")
    print("   H0 pf=%s, H1 pf=%s, alpha=beta=%s; accept H1 at LLR>=%.3f, accept H0 at LLR<=%.3f" % (p0, p1, alpha, upper, lower))
    print("   LLR step: fail %+.4f, pass %+.5f" % (llr_fail, llr_pass))
    print("   %-8s %-12s %-12s" % ("true pf", "P(quarantine)", "mean runs"))
    srng = random.Random(99)
    sims = 2000
    sprt = {}
    for true_pf in (0.0, 0.005, 0.02, 0.05, 0.2, 0.5):
        quarantined = runs_total = 0
        for _ in range(sims):
            llr, n = 0.0, 0
            while lower < llr < upper and n < 20000:
                n += 1
                llr += llr_fail if srng.random() < true_pf else llr_pass
            quarantined += 1 if llr >= upper else 0
            runs_total += n
        print("   %-8s %-12.3f %-12.1f" % (true_pf, quarantined / sims, runs_total / sims))
        sprt[true_pf] = (quarantined / sims, runs_total / sims)
    assert sprt[0.0][0] == 0.0 and sprt[0.5][0] == 1.0, "SPRT must never quarantine a clean test and must always quarantine pf=0.5"
    assert sprt[0.005][0] <= 2 * alpha and sprt[0.05][0] >= 1 - 2 * beta, "SPRT error rates must stay near alpha and beta"
    assert sprt[0.5][1] < sprt[0.2][1] < sprt[0.05][1], "worse tests must be quarantined in fewer runs"
    assert abs(sprt[0.0][1] - math.ceil(-lower / -llr_pass)) < 1e-9, "a clean test is certified after ceil(|B| / |pass step|) runs"


# ---------------------------------------------------------------- F: predictive selection
def section_f():
    rng = random.Random(31337)
    n_mod, n_test = 40, 400
    deps, tight, flaky_pf = [], [], []
    for _ in range(n_test):
        d = rng.sample(range(n_mod), 1 + rng.randrange(4))
        deps.append(d)
        tight.append({m for m in d if rng.random() < 0.25})
        flaky_pf.append(0.2 if rng.random() < 0.05 else 0.0)
    n_flaky = sum(1 for p in flaky_pf if p)

    def make_change():
        touched = rng.sample(range(n_mod), 1 + rng.randrange(3))
        bug = rng.choice(touched) if rng.random() < 0.15 else None
        cands = [t for t in range(n_test) if any(m in touched for m in deps[t])]
        return touched, bug, cands

    def outcome(t, bug, attempts=3):
        """Deterministic failure if the test is tight on the buggy module; flakes add random reds."""
        real = bug is not None and bug in tight[t]
        results = [real or rng.random() < flaky_pf[t] for _ in range(attempts)]
        return results[0], all(results)

    train = [make_change() for _ in range(3000)]
    test = [make_change() for _ in range(3000)]

    def fit(label_index):
        runs, fails = {}, {}
        for touched, bug, cands in train:
            for t in cands:
                label = outcome(t, bug)[label_index]
                for m in deps[t]:
                    if m in touched:
                        runs[(t, m)] = runs.get((t, m), 0) + 1
                        fails[(t, m)] = fails.get((t, m), 0) + (1 if label else 0)
        return runs, fails

    def score(model, t, touched):
        runs, fails = model
        return max((fails.get((t, m), 0) + 0.1) / (runs.get((t, m), 0) + 1.0) for m in deps[t] if m in touched)

    models = {"raw labels": fit(0), "deflaked labels": fit(1)}
    budget = 1.0 / 3.0
    print("F. Toy predictive test selection: %d modules, %d tests (%d flaky at pf=0.2), 3000 train + 3000 test changes" % (n_mod, n_test, n_flaky))
    print("   Select the top third of the build-dependency candidates per change by learned score.")
    print("   Truth for recall = deterministic failures (red on all 3 attempts). Build-dependency selection is recall 1.000 by construction.")
    print("   %-18s %-14s %-14s %-16s" % ("strategy", "test recall", "change recall", "flaky share"))
    results = {}
    for name in ("random", "raw labels", "deflaked labels"):
        got = total = faulty = caught = slots = flaky_slots = 0
        sel_rng = random.Random(5)
        for touched, bug, cands in test:
            k = max(1, math.ceil(budget * len(cands)))
            if name == "random":
                chosen = sel_rng.sample(cands, min(k, len(cands)))
            else:
                ranked = sorted(cands, key=lambda t: (-score(models[name], t, touched), t))
                chosen = ranked[:k]
            slots += len(chosen)
            flaky_slots += sum(1 for t in chosen if flaky_pf[t])
            truth = [t for t in cands if bug is not None and bug in tight[t]]
            if truth:
                faulty += 1
                hit = [t for t in truth if t in chosen]
                got += len(hit)
                total += len(truth)
                caught += 1 if hit else 0
        results[name] = (got / total, caught / faulty, flaky_slots / slots)
        print("   %-18s %-14.3f %-14.3f %-16.3f" % (name, *results[name]))
    assert results["deflaked labels"][1] > results["random"][1], "learned selection must beat random"
    assert results["deflaked labels"][0] > results["raw labels"][0], "flaky labels must cost recall"


def main():
    for i, section in enumerate((section_a, section_b, section_c, section_d, section_e, section_f)):
        if i:
            print()
        section()


if __name__ == "__main__":
    main()
