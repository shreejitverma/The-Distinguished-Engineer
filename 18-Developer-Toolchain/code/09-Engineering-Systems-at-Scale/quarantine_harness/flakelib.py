"""Statistics and classification primitives for the quarantine lab (stdlib only)."""
import hashlib
import math


def u01(*parts):
    """Uniform [0, 1) from a hash of the parts: reproducible on every platform and Python."""
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode("ascii")).hexdigest()
    return int(digest[:13], 16) / float(16 ** 13)


def runs_to_detect(p, confidence):
    """Smallest n with 1 - (1 - p)^n >= confidence: runs needed to see one failure of a p-flake."""
    return math.ceil(math.log(1.0 - confidence) / math.log(1.0 - p))


def zero_failure_upper(n, alpha=0.05):
    """One-sided Clopper-Pearson upper bound on the failure rate after n runs with 0 failures."""
    return 1.0 - alpha ** (1.0 / n)


def p_any_flake(n_tests, p):
    """P(at least one of n_tests independent tests, each flaky at rate p, fails in one run)."""
    return 1.0 - (1.0 - p) ** n_tests


def wilson(k, n, z=1.959964):
    """Wilson score interval for k failures in n runs (Wilson 1927)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def fisher_after_higher(k_before, n_before, k_after, n_after):
    """One-sided Fisher exact p-value that the failure rate after is higher than before."""
    total_n = n_before + n_after
    total_k = k_before + k_after
    denom = math.comb(total_n, n_after)
    num = 0
    for x in range(k_after, min(total_k, n_after) + 1):
        num += math.comb(total_k, x) * math.comb(total_n - total_k, n_after - x)
    return num / denom


def infra_runs(results, tests, commits, runs, threshold=0.15):
    """(commit, run) pairs where at least `threshold` of the whole suite failed: an infra event."""
    bad = set()
    for c in range(commits):
        for r in range(runs):
            failed = sum(1 for t in tests if results[(t, c, r)])
            if failed / len(tests) >= threshold:
                bad.add((c, r))
    return bad


def mixed_commits(results, test, commits, runs, excluded_commits):
    """Commits where the same SHA produced both a pass and a fail: Micco's definition of flaky."""
    out = []
    for c in range(commits):
        if c in excluded_commits:
            continue
        outcomes = {results[(test, c, r)] for r in range(runs)}
        if len(outcomes) == 2:
            out.append(c)
    return out


def deterministic_windows(results, test, commits, runs, min_commits):
    """Maximal commit windows where every run failed, at least min_commits long."""
    windows = []
    start = None
    for c in range(commits + 1):
        all_fail = c < commits and all(results[(test, c, r)] for r in range(runs))
        if all_fail and start is None:
            start = c
        if not all_fail and start is not None:
            if c - start >= min_commits:
                windows.append((start, c - 1))
            start = None
    return windows


def best_change_point(results, test, commits, runs, excluded_commits, min_side=5):
    """Split that most strongly says the failure rate rose; Bonferroni-adjusted over the splits."""
    keep = [c for c in range(commits) if c not in excluded_commits]
    best = None
    splits = 0
    for idx in range(min_side, len(keep) - min_side + 1):
        before = keep[:idx]
        after = keep[idx:]
        kb = sum(results[(test, c, r)] for c in before for r in range(runs))
        ka = sum(results[(test, c, r)] for c in after for r in range(runs))
        nb = len(before) * runs
        na = len(after) * runs
        p = fisher_after_higher(kb, nb, ka, na)
        splits += 1
        if best is None or p < best["p"]:
            best = {"commit": keep[idx], "p": p, "rate_before": kb / nb, "rate_after": ka / na,
                    "kb": kb, "nb": nb, "ka": ka, "na": na}
    best["p_adj"] = min(1.0, best["p"] * splits)
    best["splits"] = splits
    return best


def classify(results, test, commits, runs, excluded_commits, shift_alpha=0.001,
             shift_delta=0.15, min_window=4, min_mixed=2):
    """One label per test, in precedence order: deterministic window, shift, flaky, watch, clean."""
    fails = sum(results[(test, c, r)] for c in range(commits) if c not in excluded_commits
                for r in range(runs))
    mixed = mixed_commits(results, test, commits, runs, excluded_commits)
    windows = deterministic_windows(results, test, commits, runs, min_window)
    info = {"fails": fails, "mixed": mixed, "windows": windows, "shift": None}
    if windows:
        info["label"] = "DETERMINISTIC"
        return info
    if fails >= 3:
        cp = best_change_point(results, test, commits, runs, excluded_commits)
        if cp["p_adj"] < shift_alpha and cp["rate_after"] - cp["rate_before"] >= shift_delta:
            info["label"] = "SHIFT"
            info["shift"] = cp
            return info
    if len(mixed) >= min_mixed:
        info["label"] = "FLAKY"
    elif fails > 0:
        info["label"] = "WATCH"
    else:
        info["label"] = "CLEAN"
    return info
