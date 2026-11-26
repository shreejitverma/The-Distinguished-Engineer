"""Two real flaky tests: shared mutable state (order dependence) and random test data."""
import hashlib
import os
import time
import unittest

_REGISTRY = {}   # process-global state that outlives a single test


def _draw(seed, i, n):
    digest = hashlib.sha256(("%s:%s" % (seed, i)).encode("ascii")).hexdigest()
    return int(digest[:8], 16) % n


def _seed():
    # In the wild the default is wall-clock entropy, so a failure cannot be replayed.
    return int(os.environ.get("TEST_SEED") or time.time_ns())


class RegistryTests(unittest.TestCase):
    def test_a01_addition(self):
        self.assertEqual(2 + 2, 4)

    def test_a02_string_join(self):
        self.assertEqual("-".join(["a", "b"]), "a-b")

    def test_a03_registry_starts_empty(self):
        self.assertEqual(_REGISTRY, {})

    def test_a04_sorted(self):
        self.assertEqual(sorted([3, 1, 2]), [1, 2, 3])

    def test_a05_dict_get(self):
        self.assertIsNone({}.get("k"))

    def test_a06_slicing(self):
        self.assertEqual([1, 2, 3][1:], [2, 3])

    def test_a07_set_ops(self):
        self.assertEqual({1, 2} & {2, 3}, {2})

    def test_a08_divmod(self):
        self.assertEqual(divmod(7, 2), (3, 1))

    def test_a09_registers_plugin(self):
        self.addCleanup(_REGISTRY.clear)
        _REGISTRY["plugin"] = "enabled"
        self.assertIn("plugin", _REGISTRY)

    def test_a10_upper(self):
        self.assertEqual("ab".upper(), "AB")

    def test_a11_max(self):
        self.assertEqual(max([1, 5, 2]), 5)

    def test_a12_len(self):
        self.assertEqual(len("abcd"), 4)


class IdTests(unittest.TestCase):
    def test_ids_unique(self):
        seed = _seed()
        ids = sorted(range(16), key=lambda v: _draw(seed, v, 2 ** 31))[:4]
        self.assertEqual(len(set(ids)), 4, "seed %s drew duplicate ids %s" % (seed, ids))
