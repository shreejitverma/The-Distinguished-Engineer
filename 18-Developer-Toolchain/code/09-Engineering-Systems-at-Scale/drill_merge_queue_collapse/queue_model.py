"""Merge queue collapse drill: closed-form capacity, telemetry diagnosis, policy simulation.

Model (every number below is an assumption of THIS drill, not a measurement of any real system):
  * One round is one CI build slot of ROUND_MIN minutes of wall time.
  * PRs arrive as a Poisson stream with mean LAMBDA_ROUND per round.
  * Flake exposure is additive per PR: a build that tests k PRs together fails spuriously
    with probability 1 - (1 - f)^k.  This is the regime where affected-test selection makes the
    set of flaky tests that run grow with the batch.  The shared-suite regime (constant exposure)
    is tested for and rejected in part 2.
  * Real defects are modeled in closed form only (part 1); the simulation isolates flake.
"""

import math
import random
import sys
from collections import deque

ROUND_MIN = 30
LAMBDA_ROUND = 20.0
ROUNDS = 100
F_FLAKE = 0.03
F_QUARANTINED = 0.003
FAILURES = []


def poisson(rng, mean):
    """Knuth's multiplication method; exact for the small means used here."""
    limit = math.exp(-mean)
    k = 0
    p = 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def check(name, ok):
    print("check %-62s %s" % (name, "PASS" if ok else "FAIL"))
    if not ok:
        FAILURES.append(name)


def pct(xs, p):
    xs = sorted(xs)
    return xs[int(p * (len(xs) - 1))] if xs else 0


def simulate(policy, size, f, rounds=ROUNDS):
    """Run one policy against the shared arrival stream.

    retry:  take min(queue, size) PRs as one build; on failure put all of them back at the front.
    bisect: as retry, but a failed batch splits into halves that are rebuilt before new work.
    spec:   speculative window of `size` entries, each built on the prefix ahead of it; the first
            failure merges everything ahead of it and restarts everything behind it.
    """
    arr = random.Random(1001)
    fl = random.Random(2002)
    queue = deque()
    stack = []
    waits = []
    builds = 0
    arrived = 0
    log = []
    depths = []
    for r in range(rounds):
        n = poisson(arr, LAMBDA_ROUND)
        arrived += n
        queue.extend([r] * n)
        depth = len(queue) + sum(len(g) for g in stack)
        if r % 10 == 0:
            depths.append(depth)
        if policy == "retry":
            if not queue:
                continue
            k = min(len(queue), size)
            batch = [queue.popleft() for _ in range(k)]
            builds += 1
            ok = fl.random() < (1.0 - f) ** k
            log.append((r, depth, k, ok))
            if ok:
                waits.extend(r + 1 - a for a in batch)
            else:
                queue.extendleft(reversed(batch))
        elif policy == "bisect":
            if stack:
                g = stack.pop()
            elif queue:
                k = min(len(queue), size)
                g = [queue.popleft() for _ in range(k)]
            else:
                continue
            builds += 1
            if fl.random() < (1.0 - f) ** len(g):
                waits.extend(r + 1 - a for a in g)
            elif len(g) == 1:
                stack.append(g)
            else:
                h = len(g) // 2
                stack.append(g[h:])
                stack.append(g[:h])
        elif policy == "spec":
            if not queue:
                continue
            w = min(len(queue), size)
            window = [queue.popleft() for _ in range(w)]
            builds += w
            first_fail = w
            for i in range(w):
                if fl.random() < f:
                    first_fail = i
                    break
            waits.extend(r + 1 - a for a in window[:first_fail])
            queue.extendleft(reversed(window[first_fail:]))
        else:
            raise ValueError(policy)
    left = list(queue) + [a for g in stack for a in g]
    return {
        "arrived": arrived,
        "merged": len(waits),
        "backlog": len(left),
        "p50": pct(waits, 0.50) * ROUND_MIN,
        "p95": pct(waits, 0.95) * ROUND_MIN,
        "oldest_h": (rounds - min(left)) * ROUND_MIN / 60.0 if left else 0.0,
        "builds": builds,
        "log": log,
        "depths": depths,
    }


def part1_closed_form():
    print("== 1. Closed-form capacity of a serial queue that retries a failed batch whole ==")
    s = 1.0 - F_FLAKE
    a = -math.log(s)
    k_star = 1.0 / a
    peak = 1.0 / (math.e * a)
    print("per-PR survival s = 1 - f = %.3f, hazard a = -ln s = %.6f" % (s, a))
    print("useful PRs per build slot g(k) = k * s^k, peaks at k* = 1/a = %.2f with g(k*) = 1/(e*a) = %.2f" % (k_star, peak))
    print("%6s %12s %12s" % ("k", "P(pass)", "g(k)"))
    for k in (1, 5, 10, 20, 33, 50, 100, 200):
        print("%6d %12.4f %12.3f" % (k, s ** k, k * s ** k))
    best = max(range(1, 400), key=lambda k: k * s ** k)
    print("best integer batch size = %d, g = %.3f" % (best, best * s ** best))
    print("arrivals per build slot (lambda*T) = %.1f, utilisation at the optimum = %.2f" % (LAMBDA_ROUND, LAMBDA_ROUND / peak))
    a_max = 1.0 / (math.e * LAMBDA_ROUND)
    f_max = 1.0 - math.exp(-a_max)
    print("largest flake rate a retry-whole serial queue survives at lambda*T = %.0f: f_max = %.4f" % (LAMBDA_ROUND, f_max))
    print("peak g(k*) by flake rate f and real-defect rate d, s = (1-f)(1-d):")
    print("%8s %8s %8s %10s" % ("f", "d", "k*", "peak"))
    for f_, d_ in ((0.03, 0.0), (0.003, 0.0), (0.0, 0.02), (0.003, 0.02), (0.03, 0.02)):
        a_ = -math.log((1.0 - f_) * (1.0 - d_))
        print("%8.3f %8.3f %8.1f %10.2f" % (f_, d_, 1.0 / a_, 1.0 / (math.e * a_)))
    print("speculative window w, expected merges per round E = (1-f)(1-(1-f)^w)/f at f = %.3f:" % F_FLAKE)
    for w in (1, 5, 10, 20, 32, 40):
        e = s * (1.0 - s ** w) / F_FLAKE
        print("  w=%2d  E=%6.2f  merges per build=%.3f" % (w, e, e / w))
    need = LAMBDA_ROUND * F_FLAKE / s
    w_min = math.ceil(math.log(1.0 - need) / math.log(s))
    print("smallest window with E >= lambda*T: w_min = %d" % w_min)
    check("g(k) maximum is at the integer nearest 1/a", abs(best - k_star) <= 1.0)
    check("optimum peak is below lambda*T (collapse is unavoidable for any k)", peak < LAMBDA_ROUND)
    return s


def part2_telemetry(s_true):
    print("== 2. Diagnose the regime from telemetry: does failure grow with batch size? ==")
    rng = random.Random(3003)
    sizes = (1, 2, 4, 8, 16, 32)
    builds = []
    for _ in range(100):
        for k in sizes:
            builds.append((k, rng.random() < s_true ** k))
    print("%6s %8s %8s %10s %14s" % ("batch", "builds", "passed", "pass rate", "-ln(rate)/k"))
    for k in sizes:
        n = sum(1 for kk, _ in builds if kk == k)
        p = sum(1 for kk, ok in builds if kk == k and ok)
        rate = p / n
        print("%6d %8d %8d %10.3f %14.4f" % (k, n, p, rate, -math.log(rate) / k))

    def ll_exposure(s):
        t = 0.0
        for k, ok in builds:
            t += k * math.log(s) if ok else math.log(1.0 - s ** k)
        return t

    grid = [0.900 + 0.0005 * i for i in range(199)]
    s_hat = max(grid, key=ll_exposure)
    p_hat = sum(1 for _, ok in builds if ok) / len(builds)
    ll2 = sum(math.log(p_hat) if ok else math.log(1.0 - p_hat) for _, ok in builds)
    ll1 = ll_exposure(s_hat)
    print("H1 additive exposure P(pass) = s^k : s_hat = %.4f, log-likelihood = %.1f" % (s_hat, ll1))
    print("H2 shared suite     P(pass) = p    : p_hat = %.4f, log-likelihood = %.1f" % (p_hat, ll2))
    print("log-likelihood gap H1 - H2 = %.1f, implied per-PR flake rate f = 1 - s_hat = %.4f" % (ll1 - ll2, 1.0 - s_hat))
    check("fitted s is within 0.015 of the true per-PR survival", abs(s_hat - s_true) < 0.015)
    check("additive exposure beats shared-suite by more than 20 nats", ll1 - ll2 > 20.0)


def part3_death_spiral():
    print("== 3. The collapse: take-everything batching (cap 100), retry the whole batch ==")
    res = simulate("retry", 100, F_FLAKE)
    print("%6s %8s %8s %10s %8s" % ("round", "queued", "batch", "P(pass)", "result"))
    rows = res["log"][:8] + [None] + res["log"][-3:]
    for row in rows:
        if row is None:
            print("   ...")
            continue
        r, depth, k, ok = row
        print("%6d %8d %8d %10.4f %8s" % (r, depth, k, (1.0 - F_FLAKE) ** k, "merged" if ok else "FAILED"))
    passes = sum(1 for row in res["log"] if row[3])
    print("builds=%d passed=%d arrived=%d merged=%d backlog=%d" % (len(res["log"]), passes, res["arrived"], res["merged"], res["backlog"]))
    return res


def part4_policies(naive):
    print("== 4. Policy comparison on one arrival stream (%d rounds of %d min, lambda*T = %.0f) ==" % (ROUNDS, ROUND_MIN, LAMBDA_ROUND))
    s = 1.0 - F_FLAKE
    k_best = max(range(1, 400), key=lambda k: k * s ** k)
    rows = [
        ("naive take-all, cap 100", "retry", 100, F_FLAKE),
        ("fixed batch k*=%d, retry whole" % k_best, "retry", k_best, F_FLAKE),
        ("batch 8 + bisect", "bisect", 8, F_FLAKE),
        ("batch 32 + bisect", "bisect", 32, F_FLAKE),
        ("speculative window 20", "spec", 20, F_FLAKE),
        ("speculative window 40", "spec", 40, F_FLAKE),
        ("quarantine + naive cap 100", "retry", 100, F_QUARANTINED),
        ("quarantine + batch 32 + bisect", "bisect", 32, F_QUARANTINED),
        ("quarantine + speculative window 20", "spec", 20, F_QUARANTINED),
    ]
    print("%-36s %6s %7s %8s %8s %8s %7s %9s" % ("policy", "f", "merged", "backlog", "p50 min", "p95 min", "builds", "build/PR"))
    out = {}
    for name, pol, size, f in rows:
        r = simulate(pol, size, f)
        out[name] = r
        per = r["builds"] / r["merged"] if r["merged"] else float("inf")
        print("%-36s %6.3f %7d %8d %8d %8d %7d %9.2f" % (name, f, r["merged"], r["backlog"], r["p50"], r["p95"], r["builds"], per))
    print("arrived = %d in every row" % naive["arrived"])
    print("queue depth sampled every 10 rounds (PRs waiting):")
    names = ["naive take-all, cap 100", "speculative window 40", "quarantine + naive cap 100"]
    print("%6s %12s %12s %12s" % ("round", "naive", "spec-40", "quarantine"))
    for i in range(10):
        print("%6d %12d %12d %12d" % (i * 10, out[names[0]]["depths"][i], out[names[1]]["depths"][i], out[names[2]]["depths"][i]))
    n = out[names[0]]
    check("naive take-all merges under 40 percent of arrivals", n["merged"] < 0.40 * n["arrived"])
    check("fixed optimal batch still leaves a backlog above 300 PRs", out["fixed batch k*=%d, retry whole" % k_best]["backlog"] > 300)
    check("batch+bisect cannot fix a flake-only capacity problem", out["batch 32 + bisect"]["backlog"] > 300)
    check("quarantine alone clears the queue (backlog under 60)", out[names[2]]["backlog"] < 60)
    check("batch 8 + bisect merges fewer PRs than the naive policy", out["batch 8 + bisect"]["merged"] < n["merged"])
    check("quarantine queue depth never exceeds 25 at any sample", max(out[names[2]]["depths"]) <= 25)
    check("a window of 40 merges more than a window of 20", out[names[1]]["merged"] > out["speculative window 20"]["merged"])
    check("window 20 burns more builds per merged PR than quarantine", out["speculative window 20"]["builds"] / out["speculative window 20"]["merged"] > out["quarantine + speculative window 20"]["builds"] / out["quarantine + speculative window 20"]["merged"])


def part5_mc_check(s):
    rng = random.Random(4004)
    n = 20000
    k = 20
    ok = sum(1 for _ in range(n) if rng.random() < s ** k)
    print("== 5. Monte Carlo cross-check of P(pass) for k=%d ==" % k)
    print("closed form %.4f, simulated %.4f over %d builds" % (s ** k, ok / n, n))
    check("simulated pass rate within 0.02 of s^k", abs(ok / n - s ** k) < 0.02)


def main():
    s = part1_closed_form()
    part2_telemetry(s)
    naive = part3_death_spiral()
    part4_policies(naive)
    part5_mc_check(s)
    if FAILURES:
        print("FAILED: %d check(s)" % len(FAILURES))
        sys.exit(1)
    print("all checks passed")


main()
