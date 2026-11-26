"""Audit a gamed developer productivity dashboard, on synthetic telemetry with known ground truth.

The simulator builds 24 weeks of pull request and deployment telemetry for 40 engineers.
Thirty are in the cohort whose bonus is tied to PR count and deployment count from week 13 on.
Ten form a holdout cohort that is never paid on those counts.
Every behavior and every rate below is an ASSUMPTION written in the constants block, not a measurement of any real organization.

The auditor sees only the telemetry fields a real warehouse would hold.
Fields prefixed truth_ exist only so the lab can score the auditor, and strip() removes them before any detector runs.
A world with no incentive (the null world) goes through the same detectors and must stay quiet, otherwise a detector is not a detector.
Only the standard library is used.
"""

import math
import random
import sys
from collections import defaultdict
from statistics import median

# ---- Model assumptions (synthetic, not measured) -------------------------------------------
SEED = 20261121
N_ENG, N_BONUS, N_AREAS = 40, 30, 6
WEEKS, POLICY_WEEK, RAMP_WEEKS = 24, 13, 4
ABSTAIN = 0.15            # share of bonus-cohort engineers who refuse to game
MEDIAN_LINES = 180.0      # median lines of a logical change before any splitting
TICKET_OVERHEAD = 0.08    # true throughput lost at full gaming (rebases, review load)
PAD_PROB = 0.20           # per change, chance of an extra formatting-only PR, times intensity
TICKET_SPLIT = 0.30       # chance a later fragment gets its own ticket key, times intensity
BUDDY_PROB = 0.50         # chance a PR goes to the pre-agreed buddy reviewer, times intensity
NOOP_PROB = 0.10          # per PR, chance of an extra no-op redeploy, times intensity
INTEG_FAIL = 0.10         # chance an intermediate chain state breaks production, per boundary
REVIEW_FIXED, REVIEW_PER_LINE, REVIEW_CAP = 12.0, 0.15, 120.0   # minutes, minutes, minutes per week
ATT_FLOOR = 0.25          # lowest fraction of normal attention an overloaded reviewer gives
COMMENTS_PER_LINE = 0.06  # expected review comments per substantive changed line at full attention
DEFECT_PER_LINE = 0.0006  # latent defect probability per substantive line
ESCAPE_BASE = 0.5         # a defect escapes review with probability 1 - 0.5 * attention

# ---- Auditor thresholds ---------------------------------------------------------------------
CHAIN_GAP_H = 12.0        # same author, same area, opened within 12 h of the previous PR: one chain
RUBBER_MIN_SUBST = 20     # zero-comment approval within 3 min only counts on PRs this large
RING_MIN_COUNT, RING_SHARE = 5, 0.25
PRE = (1, 12)
POST = (13, 24)


def normal(rng):
    u1 = 1.0 - rng.random()
    u2 = rng.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def simulate(seed, gaming):
    rng = random.Random(seed)
    u = rng.random
    engs = []
    for i in range(N_ENG):
        skill = 0.7 + 0.7 * u()
        home = int(u() * N_AREAS)
        a, b = u(), u()
        engs.append({"id": i, "skill": skill, "home": home, "bonus": i < N_BONUS,
                     "prop": 0.0 if a < ABSTAIN else 0.4 + 0.6 * b})
    cohort = {True: [e["id"] for e in engs if e["bonus"]],
              False: [e["id"] for e in engs if not e["bonus"]]}
    prs, deps = [], []
    for w in range(1, WEEKS + 1):
        wk = []
        for e in engs:
            i = e["id"]
            g = 0.0
            if gaming and e["bonus"] and w >= POLICY_WEEK:
                g = e["prop"] * min(1.0, (w - POLICY_WEEK + 1) / RAMP_WEEKS)
            base = 2.0 * e["skill"] * (1.0 - TICKET_OVERHEAD * g)
            n = int(base) + (1 if u() < base - int(base) else 0)
            for t in range(n):
                total = max(5, int(MEDIAN_LINES * math.exp(0.8 * normal(rng))))
                area = e["home"] if u() < 0.8 else int(u() * N_AREAS)
                h = (w - 1) * 168 + 140.0 * u() + 8.0
                change = "c%02d-%02d-%d" % (w, i, t)
                ticket = "T%02d%02d%d" % (w, i, t)
                k = 1 + int(g * (1 + 3 * u()))
                wts = [0.5 + u() for _ in range(k)]
                s = sum(wts)
                items = []
                for j, x in enumerate(wts):
                    lines = max(1, int(round(total * x / s)))
                    items.append((lines, int(round(lines * 0.9)), j))
                if g > 0 and u() < PAD_PROB * g:
                    items.append((1 + int(8 * u()), 0, -1))
                for idx, (lines, subst, j) in enumerate(items):
                    if idx > 0:
                        h += 0.5 + 3.0 * u()
                    tk = ticket
                    if j >= 1 and g > 0 and u() < TICKET_SPLIT * g:
                        tk = "%ss%d" % (ticket, j)
                    peers = [x for x in cohort[e["bonus"]] if x != i]
                    rv = peers[int(u() * len(peers))]
                    buddy = False
                    partner = engs[i ^ 1]
                    if g > 0 and partner["bonus"] and partner["prop"] > 0 and u() < BUDDY_PROB * g:
                        rv, buddy = i ^ 1, True
                    integ = j >= 1 and u() < INTEG_FAIL
                    wk.append({"week": w, "eng": i, "ticket": tk, "area": area, "open_h": h,
                               "lines": lines, "subst": subst, "reviewer": rv,
                               "truth_change": change, "truth_buddy": buddy,
                               "truth_g": g, "truth_integ": integ})
        demand = defaultdict(float)
        for p in wk:
            if not p["truth_buddy"]:
                demand[p["reviewer"]] += REVIEW_FIXED + REVIEW_PER_LINE * p["lines"]
        for p in wk:
            if p["truth_buddy"]:
                att = 0.0
                p["comments"], p["review_min"] = 0, 1 + int(2 * u())
            else:
                att = max(ATT_FLOOR, min(1.0, REVIEW_CAP / demand[p["reviewer"]]))
                p["review_min"] = int(round((REVIEW_FIXED + REVIEW_PER_LINE * p["lines"]) * att))
                ex = COMMENTS_PER_LINE * p["subst"] * att
                p["comments"] = int(ex) + (1 if u() < ex - int(ex) else 0)
            latent = min(0.5, DEFECT_PER_LINE * p["subst"] + (0.01 if p["subst"] > 0 else 0.0))
            failed = (u() < latent) and (u() < 1.0 - ESCAPE_BASE * att)
            failed = failed or p["truth_integ"]
            rec = {"week": w, "eng": p["eng"], "subst": p["subst"], "truth_change": p["truth_change"]}
            deps.append(dict(rec, kind="pr", failed=failed))
            if failed:
                deps.append(dict(rec, kind="hotfix", failed=False, subst=10))
            if p["truth_g"] > 0 and u() < NOOP_PROB * p["truth_g"]:
                deps.append(dict(rec, kind="noop", failed=False, subst=0))
        prs.extend(wk)
    return engs, prs, deps


def strip(rows):
    return [{k: v for k, v in r.items() if not k.startswith("truth_")} for r in rows]


def count_chains(P):
    by_eng = defaultdict(list)
    for p in P:
        by_eng[p["eng"]].append(p)
    n = 0
    for eng in sorted(by_eng):
        prev = None
        for p in sorted(by_eng[eng], key=lambda r: (r["open_h"], r["lines"])):
            if prev is None or p["open_h"] - prev["open_h"] > CHAIN_GAP_H or p["area"] != prev["area"]:
                n += 1
            prev = p
    return n


def summarize(prs, deps, ids, span):
    w0, w1 = span
    idset = set(ids)
    P = [p for p in prs if w0 <= p["week"] <= w1 and p["eng"] in idset]
    D = [d for d in deps if w0 <= d["week"] <= w1 and d["eng"] in idset]
    ew = len(ids) * (w1 - w0 + 1)
    chains = count_chains(P)
    failed = sum(1 for d in D if d["failed"])
    cnt, per_author = defaultdict(int), defaultdict(int)
    for p in P:
        cnt[(p["eng"], p["reviewer"])] += 1
        per_author[p["eng"]] += 1

    def heavy(a, b):
        c = cnt.get((a, b), 0)
        return c >= RING_MIN_COUNT and c >= RING_SHARE * per_author[a]

    in_ring = [heavy(p["eng"], p["reviewer"]) and heavy(p["reviewer"], p["eng"]) for p in P]
    out = [p for p, r in zip(P, in_ring) if not r]

    return {
        "prs_ew": len(P) / ew,
        "deps_ew": len(D) / ew,
        "med_lines": median(p["lines"] for p in P),
        "cfr": 100.0 * failed / len(D),
        "empty": 100.0 * sum(1 for d in D if d["subst"] == 0) / len(D),
        "cmt100": 100.0 * sum(p["comments"] for p in P) / max(1, sum(p["subst"] for p in P)),
        "rubber": 100.0 * sum(1 for p in P if p["subst"] >= RUBBER_MIN_SUBST and p["comments"] == 0
                              and p["review_min"] <= 3) / len(P),
        "ring": 100.0 * sum(in_ring) / len(P),
        "cmt100_out": 100.0 * sum(p["comments"] for p in out) / max(1, sum(p["subst"] for p in out)),
        "pr_per_ticket": len(P) / len({p["ticket"] for p in P}),
        "pr_per_chain": len(P) / chains,
        "chains_ew": chains / ew,
        "fail100": 100.0 * failed / chains,
        "deps_per_chain": len(D) / chains,
    }


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return r


def spearman(a, b):
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb)


def verdicts(pre, post):
    return [
        ("F1 median PR size collapsed (<0.6x)", post["med_lines"] < 0.6 * pre["med_lines"]),
        ("F2 PRs per reconstructed chain (>=1.5x)", post["pr_per_chain"] >= 1.5 * pre["pr_per_chain"]),
        ("F3 comments per 100 lines (<0.8x)", post["cmt100"] < 0.8 * pre["cmt100"]),
        ("F4 rubber-stamp share (+10 points)", post["rubber"] - pre["rubber"] >= 10.0),
        ("F5 approval ring share (>=5%, +5 points)", post["ring"] >= 5.0 and post["ring"] - pre["ring"] >= 5.0),
        ("F6 failures per 100 changes (>=1.5x)", post["fail100"] >= 1.5 * pre["fail100"]),
        ("F7 deploys per change (>=1.5x), empty +5", post["deps_per_chain"] >= 1.5 * pre["deps_per_chain"]
         and post["empty"] - pre["empty"] >= 5.0),
        ("F8 activity up 1.5x while failures worse 1.2x", post["prs_ew"] >= 1.5 * pre["prs_ew"]
         and post["fail100"] > 1.2 * pre["fail100"]),
    ]


def engineer_vectors(prs, deps, ids, span):
    w0, w1 = span
    pc, dc, tc = defaultdict(int), defaultdict(int), defaultdict(set)
    for p in prs:
        if w0 <= p["week"] <= w1:
            pc[p["eng"]] += 1
            tc[p["eng"]].add(p["truth_change"])
    for d in deps:
        if w0 <= d["week"] <= w1:
            dc[d["eng"]] += 1
    bonus = [pc[i] + dc[i] for i in ids]
    true_changes = [len(tc[i]) for i in ids]
    return bonus, true_changes


def chains_per_engineer(prs, ids, span):
    w0, w1 = span
    return [count_chains([p for p in prs if p["eng"] == i and w0 <= p["week"] <= w1]) for i in ids]


def main():
    engs, prs_t, deps_t = simulate(SEED, True)
    _, nprs_t, ndeps_t = simulate(SEED, False)
    prs, deps = strip(prs_t), strip(deps_t)
    nprs, ndeps = strip(nprs_t), strip(ndeps_t)
    bonus_ids = [e["id"] for e in engs if e["bonus"]]
    hold_ids = [e["id"] for e in engs if not e["bonus"]]

    print("== Synthetic world (every rate is a model assumption, not a measurement) ==")
    print("engineers: %d (bonus cohort %d, holdout %d), weeks: %d, bonus on PR and deploy counts from week %d"
          % (N_ENG, N_BONUS, N_ENG - N_BONUS, WEEKS, POLICY_WEEK))
    print("PRs generated: %d (gamed world), %d (null world); deployments: %d, %d"
          % (len(prs), len(nprs), len(deps), len(ndeps)))
    print()

    print("== 1. What the executive dashboard shows (bonus cohort, 4-week blocks) ==")
    print("weeks    PRs/eng-wk  deploys/eng-wk  change-fail-rate%  median-PR-lines")
    for b in range(WEEKS // 4):
        s = summarize(prs, deps, bonus_ids, (4 * b + 1, 4 * b + 4))
        print("%2d-%-2d    %10.2f  %14.2f  %17.1f  %15d"
              % (4 * b + 1, 4 * b + 4, s["prs_ew"], s["deps_ew"], s["cfr"], s["med_lines"]))
    print()

    bp, bq = summarize(prs, deps, bonus_ids, PRE), summarize(prs, deps, bonus_ids, POST)
    hp, hq = summarize(prs, deps, hold_ids, PRE), summarize(prs, deps, hold_ids, POST)
    print("== 2. Pre (weeks 1-12) vs post (weeks 13-24), with the holdout as the control group ==")
    print("%-34s %9s %9s %9s %9s %9s" % ("metric", "bonus pre", "bonus pst", "hold pre", "hold pst", "DiD"))
    rows = [("PRs per engineer-week", "prs_ew", "%.2f"),
            ("median lines per PR", "med_lines", "%.0f"),
            ("deploys per engineer-week", "deps_ew", "%.2f"),
            ("dashboard change fail rate %", "cfr", "%.1f"),
            ("empty-diff deploys %", "empty", "%.1f"),
            ("comments per 100 substantive lines", "cmt100", "%.2f"),
            ("rubber-stamp approvals %", "rubber", "%.1f"),
            ("approval-ring PRs %", "ring", "%.1f"),
            ("PRs per ticket key", "pr_per_ticket", "%.2f"),
            ("PRs per reconstructed chain", "pr_per_chain", "%.2f"),
            ("failed deploys per 100 changes", "fail100", "%.1f")]
    for label, key, fmt in rows:
        did = (bq[key] - bp[key]) - (hq[key] - hp[key])
        cells = [fmt % bp[key], fmt % bq[key], fmt % hp[key], fmt % hq[key], "%+.2f" % did]
        print("%-34s %9s %9s %9s %9s %9s" % ((label,) + tuple(cells)))
    print()

    gamed_b = verdicts(bp, bq)
    gamed_h = verdicts(hp, hq)
    np_, nq_ = (summarize(nprs, ndeps, bonus_ids, PRE), summarize(nprs, ndeps, bonus_ids, POST))
    null_b = verdicts(np_, nq_)
    print("== 3. Detector verdicts (FLAG = pathology present) ==")
    print("%-48s %-11s %-11s %-9s" % ("detector", "gamed bonus", "gamed hold", "null"))
    for (name, a), (_, b), (_, c) in zip(gamed_b, gamed_h, null_b):
        print("%-48s %-11s %-11s %-9s" % (name, "FLAG" if a else "ok", "FLAG" if b else "ok",
                                          "FLAG" if c else "ok"))
    print()

    print("F3 note: bonus comments per 100 lines post/pre = %.2f, holdout = %.2f (bar is 0.80); "
          "outside the ring the bonus cohort goes %.2f -> %.2f"
          % (bq["cmt100"] / bp["cmt100"], hq["cmt100"] / hp["cmt100"], bp["cmt100_out"], bq["cmt100_out"]))
    print()

    print("== 4. Reconstruction accuracy (ground truth exists only inside the simulator) ==")
    for label, span in (("pre ", PRE), ("post", POST)):
        T = [p for p in prs_t if span[0] <= p["week"] <= span[1] and p["eng"] in set(bonus_ids)]
        D = [d for d in deps_t if span[0] <= d["week"] <= span[1] and d["eng"] in set(bonus_ids)]
        true_n = len({p["truth_change"] for p in T})
        s = summarize(prs, deps, bonus_ids, span)
        chains = round(s["chains_ew"] * len(bonus_ids) * (span[1] - span[0] + 1))
        tickets = len({p["ticket"] for p in strip(T)})
        failed = sum(1 for d in D if d["failed"])
        print("%s: true changes %d, reconstructed chains %d (%+.1f%%), ticket keys %d (%+.1f%%), "
              "failures per 100 true changes %.1f"
              % (label, true_n, chains, 100.0 * (chains - true_n) / true_n, tickets,
                 100.0 * (tickets - true_n) / true_n, 100.0 * failed / true_n))
    print()

    print("== 5. Metric validity: does a per-engineer score track delivered changes? (bonus cohort, Spearman) ==")
    rho = {}
    for key, label, span in (("pre", "pre ", PRE), ("post", "post", POST)):
        score, truth = engineer_vectors(prs_t, deps_t, bonus_ids, span)
        rebuilt = chains_per_engineer(prs, bonus_ids, span)
        rho[key] = (spearman(score, truth), spearman(rebuilt, truth))
        print("%s: bonus score (PRs + deploys) %+.2f, reconstructed changes %+.2f (both vs true changes delivered)"
              % (label, rho[key][0], rho[key][1]))
    print()

    print("== 6. Outcome-first scorecard, bonus cohort (each activity metric beside its counter-metric) ==")
    print("%-42s %9s %9s" % ("metric", "pre", "post"))
    for label, key, fmt in (("PRs per engineer-week (activity)", "prs_ew", "%.2f"),
                            ("PRs per reconstructed change", "pr_per_chain", "%.2f"),
                            ("deploys per engineer-week (activity)", "deps_ew", "%.2f"),
                            ("deploys per reconstructed change", "deps_per_chain", "%.2f"),
                            ("dashboard change fail rate %", "cfr", "%.1f"),
                            ("failed deploys per 100 reconstructed changes", "fail100", "%.1f"),
                            ("reconstructed changes per engineer-week", "chains_ew", "%.2f"),
                            ("comments per 100 lines, all PRs", "cmt100", "%.2f"),
                            ("comments per 100 lines, outside the ring", "cmt100_out", "%.2f")):
        print("%-42s %9s %9s" % (label, fmt % bp[key], fmt % bq[key]))

    expected = {"F1", "F2", "F4", "F5", "F6", "F7", "F8"}
    got = {name.split()[0] for name, f in gamed_b if f}
    quiet = not any(f for _, f in gamed_h) and not any(f for _, f in null_b)
    ok = (got == expected and quiet
          and rho["post"][0] < rho["pre"][0] - 0.1 and rho["post"][1] > rho["post"][0])
    if not ok:
        sys.stderr.write("SELF-CHECK FAILED: flagged %s, expected %s; holdout and null quiet: %s; "
                         "rho %s\n" % (sorted(got), sorted(expected), quiet, rho))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
