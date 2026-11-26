"""Deterministic synthetic CI history: 40 commits, 2 full-suite runs per commit, 120 tests.

Every outcome is a hash of (test, commit, run), so the lab prints the same numbers everywhere.
The failure model per test is explicit in fail_prob(); the harness never reads it when it
classifies, only when it scores the classification against the truth.
"""
import datetime

from flakelib import u01

N_COMMITS = 40
N_RUNS = 2
INFRA_COMMIT = 17
INFRA_P = 0.35
BASE_DATE = datetime.date(2026, 11, 2)

FLAKY_CHECKOUT = "flaky_checkout"
FLAKY_CACHE_TTL = "flaky_cache_ttl"
FLAKY_RATE_WINDOW = "flaky_rate_window"
FLAKY_REPORT_RENDER = "flaky_report_render"
REGRESS_LEDGER = "regress_ledger"
SHIFT_ORDERS_DEDUP = "shift_orders_dedup"
SPECIAL = [FLAKY_CHECKOUT, FLAKY_CACHE_TTL, FLAKY_RATE_WINDOW, FLAKY_REPORT_RENDER,
           REGRESS_LEDGER, SHIFT_ORDERS_DEDUP]
TESTS = SPECIAL + ["stable_%03d" % i for i in range(1, 115)]

OWNERS = {  # CODEOWNERS-style: test name prefix to owning team
    "flaky_checkout": "team-payments", "flaky_cache_ttl": "team-platform",
    "flaky_rate_window": "team-platform", "flaky_report_render": "team-reporting",
    "regress_ledger": "team-payments", "shift_orders_dedup": "team-orders",
}


def author_of(commit):
    return "dev-" + "abcde"[commit % 5]


def commit_date(commit):
    return BASE_DATE + datetime.timedelta(days=commit)


def fail_prob(test, commit):
    """Ground truth failure probability, hidden from the classifier."""
    if test == FLAKY_CHECKOUT:
        return 0.30
    if test == FLAKY_CACHE_TTL:
        return 0.10 if commit < 30 else 1.0   # a real break lands on top of an old flake
    if test == FLAKY_RATE_WINDOW:
        return 0.02
    if test == FLAKY_REPORT_RENDER:
        return 0.005
    if test == REGRESS_LEDGER:
        return 1.0 if 24 <= commit <= 28 else 0.0  # broken for 5 commits, then fixed forward
    if test == SHIFT_ORDERS_DEDUP:
        return 0.5 if commit >= 28 else 0.0       # commit 28 introduced a race
    return 0.0


def fails(test, commit, run):
    if commit == INFRA_COMMIT and run == 0 and u01("infra", test, commit) < INFRA_P:
        return True   # correlated outage: independent of the test's own behavior
    return u01("t", test, commit, run) < fail_prob(test, commit)


def build():
    return {(t, c, r): fails(t, c, r) for t in TESTS for c in range(N_COMMITS) for r in range(N_RUNS)}
