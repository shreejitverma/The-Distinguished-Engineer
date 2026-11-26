#!/usr/bin/env python3
"""Run named unittest methods in a given order inside ONE process, so shared state leaks.

    runner.py --dir DIR --module MOD --cls CLS --order a,b,c
    runner.py --dir DIR --module MOD --cls CLS --order a --seeds 0-199   (sets TEST_SEED per run)

Prints one JSON object on stdout: {"failed": [...]} or {"failed_seeds": [...]}.
"""
import argparse
import json
import os
import sys
import unittest


def run(cls, names):
    result = unittest.TestResult()
    unittest.TestSuite(cls(n) for n in names).run(result)
    return [test.id().split(".")[-1] for test, _ in result.failures + result.errors]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--module", required=True)
    ap.add_argument("--cls", required=True)
    ap.add_argument("--order", default="")
    ap.add_argument("--seeds", default="")
    args = ap.parse_args()
    sys.path.insert(0, args.dir)
    cls = getattr(__import__(args.module), args.cls)
    names = [n for n in args.order.split(",") if n]
    if args.seeds:
        lo, hi = args.seeds.split("-")
        failed_seeds = []
        for seed in range(int(lo), int(hi) + 1):
            os.environ["TEST_SEED"] = str(seed)
            if run(cls, names):
                failed_seeds.append(seed)
        print(json.dumps({"failed_seeds": failed_seeds}))
    else:
        print(json.dumps({"failed": run(cls, names)}))


if __name__ == "__main__":
    main()
