"""Shared pytest configuration for 20-API-Engineering labs."""

from __future__ import annotations

import pytest

TIER_MARKERS = frozenset({"unit", "socket"})


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Fail collection unless every lab test carries exactly one execution tier marker."""
    problems: list[str] = []
    for item in items:
        tiers = sorted({mark.name for mark in item.iter_markers() if mark.name in TIER_MARKERS})
        if len(tiers) != 1:
            problems.append(f"{item.nodeid}: tier markers {tiers}")
    if problems:
        expected = " or ".join(f"@pytest.mark.{name}" for name in sorted(TIER_MARKERS))
        raise pytest.UsageError(
            f"every lab test needs exactly one of {expected}:\n" + "\n".join(problems)
        )
