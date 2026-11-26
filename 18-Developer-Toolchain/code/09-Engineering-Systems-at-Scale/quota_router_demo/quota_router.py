"""Models behind the Multi-Agent Supervision and Quota-Aware Routing note.

Everything here is a deterministic model with an explicit seed.
Nothing is a measurement of a real vendor, a real fleet, or a real review team.
Each section ends in checks that raise SystemExit if the mechanism is absent.
"""
import math
from collections import deque


class Rng:
    """64-bit LCG (Knuth MMIX constants); no dependence on the random module."""

    def __init__(self, seed):
        self.x = seed & 0xFFFFFFFFFFFFFFFF

    def u(self):
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        return (self.x >> 11) / float(1 << 53)

    def expo(self, rate):
        return -math.log(1.0 - self.u()) / rate

    def below(self, n):
        return int(self.u() * n)


def check(cond, msg):
    if not cond:
        raise SystemExit("CHECK FAILED: " + msg)


# ---------------------------------------------------------------- 1. router

INF = float("inf")


class Provider:
    """One quota window. Providers sharing a bucket draw on the same window."""

    def __init__(self, name, bucket, window_h, used_pct, elapsed_h):
        self.name, self.bucket = name, bucket
        self.window_h, self.used_pct, self.elapsed_h = window_h, used_pct, elapsed_h

    def known(self):
        return self.used_pct is not None

    def remaining(self):
        return 100.0 - self.used_pct

    def burn(self):
        return self.used_pct / self.elapsed_h if self.elapsed_h > 0 else 0.0

    def hours_to_exhaust(self):
        b = self.burn()
        return INF if b <= 0 else self.remaining() / b

    def hours_to_reset(self):
        return self.window_h - self.elapsed_h

    def spend_priority(self):
        """Projected percent of the window left unused at reset (use it or lose it)."""
        return max(0.0, self.remaining() - self.burn() * self.hours_to_reset())

    def viable(self, task_pct, task_h):
        if not self.known():
            return True  # unknown stays eligible but is ranked below every known peer
        if self.remaining() <= 0:
            return False  # exhausted_now
        if self.remaining() < task_pct:
            return False
        return self.hours_to_exhaust() >= task_h  # not projected to exhaust mid-task


TIER_COST = {1: (6.0, 2.0), 2: (3.0, 1.0), 3: (0.5, 0.25)}  # (pct of window, hours)
TIER1_ORDER = ["fable", "opus", "grok", "gemini"]  # strict fallback chain
PEERS = {2: ["opus", "grok", "gemini"], 3: ["haiku", "grok", "gemini"]}


def route(tier, provs, balance=True):
    """Return (provider name or None, reason)."""
    pct, hrs = TIER_COST[tier]
    if tier == 1 and balance:
        for name in TIER1_ORDER:  # fit first; never balance by spend priority
            p = provs[name]
            if p.viable(pct, hrs):
                return name, "tier1 chain position %d" % (TIER1_ORDER.index(name) + 1)
        return None, "STOP: no tier1 candidate has runway"
    names = TIER1_ORDER if tier == 1 else PEERS[tier]
    live = [provs[n] for n in names if provs[n].viable(pct, hrs)]
    if not live:
        return None, "STOP: no candidate has runway"
    known = [p for p in live if p.known()]
    pool = known if known else live
    best = max(pool, key=lambda p: (p.spend_priority() if p.known() else 0.0, -names.index(p.name)))
    return best.name, "spend_priority %.1f" % (best.spend_priority() if best.known() else 0.0)


def make_fleet():
    # name, bucket, window_h, used_pct, elapsed_h
    spec = [
        ("fable", "fable-week", 168, 30.0, 84),
        ("opus", "shared-week", 168, 55.0, 84),
        ("haiku", "shared-week", 168, 55.0, 84),
        ("grok", "grok-month", 720, 20.0, 360),
        ("gemini", "gemini-day", 24, 10.0, 12),
    ]
    return {n: Provider(n, b, w, u, e) for n, b, w, u, e in spec}


def charge(provs, name, pct):
    bucket = provs[name].bucket
    for p in provs.values():
        if p.bucket == bucket:
            p.used_pct += pct


def section_router():
    print("== 1. quota-aware router ==")
    provs = make_fleet()
    print("%-7s %-11s %5s %6s %8s %8s" % ("name", "bucket", "used%", "burn/h", "h_exh", "spend_pr"))
    for p in provs.values():
        print("%-7s %-11s %5.1f %6.3f %8.1f %8.1f" % (
            p.name, p.bucket, p.used_pct, p.burn(), p.hours_to_exhaust(), p.spend_priority()))
    print("dispatch of 10 tasks (tier: choice, reason)")
    seq = [2, 2, 3, 1, 2, 3, 2, 1, 2, 3]
    chosen = []
    for i, tier in enumerate(seq, 1):
        name, why = route(tier, provs)
        chosen.append((tier, name))
        print("  %2d  t%d -> %-7s %s" % (i, tier, name, why))
        if name:
            charge(provs, name, TIER_COST[tier][0])
    print("end-state used%%: %s" % " ".join(
        "%s=%.1f" % (b, next(p.used_pct for p in provs.values() if p.bucket == b))
        for b in sorted({p.bucket for p in provs.values()})))
    check(all(n == "fable" for t, n in chosen if t == 1), "tier1 left the frontier model while it had runway")
    check(len({n for t, n in chosen if t == 2}) > 1, "tier2 never balanced across peers")

    print("edge cases")
    # a) frontier window exhausted: tier1 falls to the next chain entry, not to the biggest-headroom peer
    p = make_fleet(); p["fable"].used_pct = 100.0
    name, why = route(1, p)
    naive, _ = route(1, p, balance=False)
    print("  fable exhausted: chain -> %s, naive balancer -> %s" % (name, naive))
    check(name == "opus" and naive != name, "tier1 fallback order or the naive contrast failed")
    # b) every tier1 candidate exhausted: stop and report instead of downgrading
    p = make_fleet()
    for n in TIER1_ORDER:
        p[n].used_pct = 100.0
    name, why = route(1, p)
    print("  all tier1 exhausted: %s | %s" % (name, why))
    check(name is None, "tier1 downgraded instead of stopping")
    # c) unknown runway is eligible but never outranks known evidence
    p = make_fleet(); p["grok"].used_pct = None
    name, why = route(2, p)
    print("  grok unknown: tier2 -> %s" % name)
    check(name != "grok", "unknown runway outranked a known peer")
    # c2) unknown must lose to a known peer even when that peer's spend priority is 0 and the
    # unknown sits earlier in the chain (a tie broken by chain order would pick grok)
    p = make_fleet(); p["grok"].used_pct = None
    p["fable"].used_pct = p["opus"].used_pct = 100.0
    p["gemini"].used_pct, p["gemini"].elapsed_h = 50.0, 12.0
    check(p["gemini"].spend_priority() == 0.0 and p["gemini"].viable(*TIER_COST[1]), "c2 setup is wrong")
    name, _ = route(1, p, balance=False)
    check(name == "gemini", "unknown runway outranked a known peer with zero spend priority")
    # d) a candidate that would exhaust mid-task is dropped
    p = make_fleet(); p["grok"].used_pct, p["grok"].elapsed_h = 40.0, 2.0
    live = p["grok"].viable(*TIER_COST[2])
    print("  grok burn %.1f%%/h with 60%% left: viable for a 1h task = %s" % (p["grok"].burn(), live))
    check(live, "a candidate with 3h of runway was dropped for a 1h task")
    p["gemini"].used_pct, p["gemini"].elapsed_h = 80.0, 2.0
    print("  gemini burn %.1f%%/h with 20%% left: viable for a 1h task = %s" % (
        p["gemini"].burn(), p["gemini"].viable(*TIER_COST[2])))
    check(not p["gemini"].viable(*TIER_COST[2]), "projected mid-task exhaustion was not detected")
    # e) shared bucket: spending on one model drains its sibling
    p = make_fleet(); before = p["haiku"].used_pct
    charge(p, "opus", 3.0)
    print("  shared bucket: haiku used%% %.1f -> %.1f after charging opus" % (before, p["haiku"].used_pct))
    check(p["haiku"].used_pct == before + 3.0, "shared bucket did not propagate")


# ------------------------------------------------------- 2. retry herding

def retry_sim(policy, n=100, cap=10, seed=7, max_slots=400):
    """n workers hit a server that admits cap requests per 1-second slot."""
    rng = Rng(seed)
    due = [0] * n  # slot of the next attempt
    attempts_n = [0] * n
    done = [False] * n
    total, peak, last = 0, 0, 0
    for slot in range(max_slots):
        arrivals = [i for i in range(n) if not done[i] and due[i] == slot]
        if arrivals:
            if slot > 0:
                peak = max(peak, len(arrivals))  # retry waves only; slot 0 is the initial herd
            total += len(arrivals)
            for k, i in enumerate(arrivals):
                if k < cap:
                    done[i] = True
                    last = slot
                else:
                    attempts_n[i] += 1
                    a = attempts_n[i]
                    base = min(64, 2 ** a)
                    if policy == "fixed":
                        d = 1
                    elif policy == "exp":
                        d = base
                    else:  # full jitter
                        d = 1 + rng.below(base)
                    due[i] = slot + d
        if all(done):
            break
    return total, peak, last + 1


def section_retry():
    print("== 2. retry herding (100 workers, server admits 10 per slot) ==")
    print("%-14s %8s %14s %7s" % ("policy", "attempts", "peak retry/slot", "slots"))
    res = {}
    for pol in ("fixed", "exp", "full-jitter"):
        res[pol] = retry_sim(pol)
        print("%-14s %8d %14d %7d" % ((pol,) + res[pol]))
    check(res["full-jitter"][0] < res["exp"][0] and res["full-jitter"][0] < res["fixed"][0],
          "jitter did not cut attempts")
    check(res["full-jitter"][2] < res["exp"][2], "jitter did not beat unjittered exponential on completion time")
    check(res["full-jitter"][1] < res["fixed"][1], "jitter did not flatten the retry peak")
    check(res["fixed"][0] == 550, "fixed-delay attempts should be 100+90+...+10 = 550")


# ------------------------------------------------- 3. restart intensity

def supervise(crashes, intensity, period):
    """OTP rule: more than `intensity` restarts in the last `period` seconds => give up."""
    window = deque()
    for t in crashes:
        while window and window[0] <= t - period:
            window.popleft()
        window.append(t)
        if len(window) > intensity:
            return "ESCALATE at t=%d after %d restarts in %ds" % (t, len(window) - 1, period)
    return "survived %d crashes" % len(crashes)


def section_supervisor():
    print("== 3. supervisor restart intensity ==")
    quick = [0, 1, 2]           # 3 crashes in 2 seconds
    slow = list(range(0, 300, 7))  # one crash every 7 s for 300 s
    for inten, per in ((5, 30), (1, 6), (10, 60)):
        print("  intensity=%2d period=%2ds sustained<=%.2f/s | quick burst: %s | steady every 7s: %s" % (
            inten, per, inten / per, supervise(quick, inten, per), supervise(slow, inten, per)))
    check(supervise(quick, 5, 30).startswith("survived"), "5/30 should absorb a burst of 3")
    check(supervise(quick, 1, 6).startswith("ESCALATE"), "1/6 should reject a quick second restart")
    check(supervise(slow, 5, 30).startswith("survived"), "5/30 should tolerate one crash per 7s")
    check(supervise(quick, 10, 60).startswith("survived"), "10/60 should absorb a burst of 3")
    check(supervise(slow, 10, 60).startswith("survived"), "10/60 should tolerate one crash per 7s")
    check(supervise(slow, 1, 6).startswith("survived"), "1/6 should tolerate one crash per 7s")
    print("  nested 10 x 10 supervisors allow up to %d restarts before the top gives up" % (10 * 10))


# ------------------------------------------------------ 4. review queue

def queue_sim(lam_h, lam_a, mu, days, cap=None, seed=11):
    """Single FIFO reviewer pool, rates per day. cap: reject agent PRs when >= cap in system."""
    rng = Rng(seed)
    arrivals = []
    for cls, lam in (("h", lam_h), ("a", lam_a)):
        t = 0.0
        while lam > 0:
            t += rng.expo(lam)
            if t > days:
                break
            arrivals.append((t, cls))
    arrivals.sort()
    deps, last_dep = deque(), 0.0
    soj = {"h": [], "a": []}
    sent_a = rejected_a = 0
    for t, cls in arrivals:
        while deps and deps[0] <= t:
            deps.popleft()
        if cls == "a":
            sent_a += 1
            if cap is not None and len(deps) >= cap:
                rejected_a += 1
                continue
        start = max(t, last_dep)
        last_dep = start + rng.expo(mu)
        deps.append(last_dep)
        soj[cls].append(last_dep - t)
    mean_h = sum(soj["h"]) / len(soj["h"]) * 8.0  # working hours per day = 8
    return mean_h, sent_a, rejected_a


def section_queue():
    print("== 4. review queue saturation (mu=40 reviews/day, 8 working hours/day) ==")
    mu, lam_h = 40.0, 24.0
    print("%8s %6s %12s %14s" % ("lam_agent", "rho", "W analytic h", "W simulated h"))
    prev = 0.0
    for lam_a in (0.0, 8.0, 12.0, 14.0, 15.0):
        lam = lam_h + lam_a
        rho = lam / mu
        w = 8.0 / (mu - lam)
        sim, _, _ = queue_sim(lam_h, lam_a, mu, 4000)
        print("%8.1f %6.3f %12.2f %14.2f" % (lam_a, rho, w, sim))
        check(w > prev, "analytic wait must rise with load")
        prev = w
    print("overload (lam_agent=20 => rho=1.1), human PR mean sojourn in working hours")
    s50, _, _ = queue_sim(lam_h, 20.0, mu, 50)
    s500, _, _ = queue_sim(lam_h, 20.0, mu, 500)
    print("  no cap: 50 days -> %.1f h, 500 days -> %.1f h" % (s50, s500))
    c50, sent, rej = queue_sim(lam_h, 20.0, mu, 50, cap=6)
    c500, sent5, rej5 = queue_sim(lam_h, 20.0, mu, 500, cap=6)
    print("  agent WIP cap=6: 50 days -> %.1f h, 500 days -> %.1f h" % (c50, c500))
    print("  agent PRs rejected at the cap over 500 days: %d of %d (%.0f%%)" % (rej5, sent5, 100.0 * rej5 / sent5))
    check(s500 > 3 * s50, "uncapped queue should keep growing past rho=1")
    check(c500 < 2.0 * c50 + 5.0 and c500 < s500 / 10, "cap did not bound the wait")


# ------------------------------------------------------ 5. verification

def section_verify():
    print("== 5. verifier independence (model, assumed parameters) ==")
    rng = Rng(99)
    n, p_wrong = 200000, 0.15
    m = 0.4  # one verifier misses a wrong claim with probability 0.4 (assumption)
    print("  one verifier miss rate m=%.2f; two verifiers, common-cause coupling rho" % m)
    print("  %5s %12s %12s" % ("rho", "P(both miss)", "simulated"))
    out = {}
    for rho in (0.0, 0.5, 1.0):
        both = wrong = 0
        for _ in range(n):
            if rng.u() >= p_wrong:
                continue
            wrong += 1
            miss1 = rng.u() < m
            miss2 = miss1 if rng.u() < rho else rng.u() < m
            both += 1 if (miss1 and miss2) else 0
        formula = m * (rho + (1 - rho) * m)
        out[rho] = both / wrong
        print("  %5.1f %12.3f %12.3f" % (rho, formula, both / wrong))
        check(abs(out[rho] - formula) < 0.01, "simulation disagrees with closed form at rho=%.1f" % rho)
    check(out[0.0] < out[0.5] < out[1.0], "coupling must erode the benefit of a second verifier")
    resid = p_wrong * out[0.5]
    print("  residual wrong-claim rate with 2 verifiers at rho=0.5: %.3f (producer alone %.3f)" % (resid, p_wrong))


if __name__ == "__main__":
    section_router()
    section_retry()
    section_supervisor()
    section_queue()
    section_verify()
    print("ALL CHECKS PASSED")
