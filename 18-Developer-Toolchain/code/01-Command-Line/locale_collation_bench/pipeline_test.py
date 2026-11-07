#!/usr/bin/env python3
"""Sections 5-8 of the locale lab: sort, join, comm, and filesystem normalization.

Every section computes its result from the tools' real output and exits non-zero
(via check) when the mechanism it demonstrates is absent.
"""
import os
import subprocess
import sys
import tempfile

C_ENV = {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}
UTF8_ENV = {"LC_ALL": "en_US.UTF-8", "PATH": "/usr/bin:/bin"}


def check(cond, msg):
    if not cond:
        print(f"CHECK FAILED: {msg}", file=sys.stderr)
        sys.exit(1)


def run(argv, env, stdin=None):
    return subprocess.run(argv, input=stdin, text=True, capture_output=True, env=env)


def lines(text):
    return text.splitlines()


def test_pipeline():
    print("\n=== 5. CLI Tool Collation Order (sort) ===")
    items = ["Apple", "apple", "Banana", "banana", "a-1", "a1", "b-1", "b1"]
    raw_input = "\n".join(items) + "\n"

    r = run(["sort"], C_ENV, raw_input)
    check(r.returncode == 0, "sort under C failed")
    sorted_c = lines(r.stdout)
    print("LC_ALL=C sort order:")
    for line in sorted_c:
        print(f"  {line}")

    r = run(["sort"], UTF8_ENV, raw_input)
    check(r.returncode == 0, "sort under en_US.UTF-8 failed")
    sorted_utf8 = lines(r.stdout)
    print("LC_ALL=en_US.UTF-8 sort order:")
    for line in sorted_utf8:
        print(f"  {line}")

    check(sorted_c == sorted(items, key=lambda s: s.encode()), "C sort is not byte order")
    check(sorted_utf8 != sorted_c, "locale sort equals byte order")

    print("\n=== 6. Relational Join Integrity Across Sort Locales (join) ===")
    with tempfile.TemporaryDirectory() as td:
        f1 = os.path.join(td, "f1.txt")
        f2 = os.path.join(td, "f2.txt")
        with open(f1, "w") as f:
            f.write("Apple 100\nBanana 200\napple 300\nbanana 400\n")
        with open(f2, "w") as f:
            f.write("Apple fruit\nBanana fruit\napple fruit\nbanana fruit\n")
        keys = {ln.split()[0] for ln in lines(open(f1).read())}

        f1_c = os.path.join(td, "f1_c.txt")
        f2_c = os.path.join(td, "f2_c.txt")
        f1_en = os.path.join(td, "f1_en.txt")
        for env, src, dst in ((C_ENV, f1, f1_c), (C_ENV, f2, f2_c), (UTF8_ENV, f1, f1_en)):
            check(run(["sort", src, "-o", dst], env).returncode == 0, "sort -o failed")

        base = run(["join", f1_c, f2_c], C_ENV)
        base_lines = lines(base.stdout)
        print(f"Matching LC_ALL=C sort and join: {len(base_lines)}/4 rows matched")
        for line in base_lines:
            print(f"  {line}")
        check(base.returncode == 0 and len(base_lines) == 4, "baseline join lost rows")

        bad = run(["join", f1_en, f2_c], C_ENV)
        bad_lines = lines(bad.stdout)
        print(f"Mismatched sort locales in join: {len(bad_lines)}/4 rows matched (silent data loss)")
        for line in bad_lines:
            print(f"  {line}")
        dropped = sorted(keys - {ln.split()[0] for ln in bad_lines})
        print(f"Dropped record keys (computed): {', '.join(dropped)}")
        print(f"join exit status with out-of-order input (BSD join on macOS): {bad.returncode}")
        check(dropped == ["Banana"], f"expected Banana to be dropped, got {dropped}")
        check(bad.returncode == 0, "BSD join no longer exits 0 on unsorted input")

        print("\n=== 7. Set Intersection Integrity (comm) ===")
        comm = run(["comm", "-12", f1_en, f1_c], C_ENV)
        comm_lines = lines(comm.stdout)
        print(f"Mismatched sort locales in comm -12: {len(comm_lines)}/4 common rows detected")
        for line in comm_lines:
            print(f"  {line}")
        missing = sorted(set(lines(open(f1).read())) - set(comm_lines))
        print(f"Dropped common records (computed): {', '.join(missing)}")
        print(f"comm exit status with out-of-order input (BSD comm on macOS): {comm.returncode}")
        check(missing == ["Banana 200"], f"expected Banana 200 to be dropped, got {missing}")
        check(comm.returncode == 0, "BSD comm no longer exits 0 on unsorted input")

        print("\n=== 8. Filesystem Unicode Normalization (macOS APFS) ===")
        nfc_rel = "item_é.txt"  # U+00E9 precomposed
        nfd_rel = "item_é.txt"  # U+0065 U+0301 decomposed
        with open(os.path.join(td, nfc_rel), "w") as f:
            f.write("payload\n")

        nfd_accessible = os.path.exists(os.path.join(td, nfd_rel))
        print(f"APFS lookup via decomposed NFD path: {'SUCCESS' if nfd_accessible else 'FAILURE'}")

        entry = [e for e in os.listdir(td) if e.startswith("item_")][0]
        is_nfc = entry.encode("utf-8") == nfc_rel.encode("utf-8")
        print(f"APFS on-disk directory record preserved form: {'NFC (raw bytes intact)' if is_nfc else 'NFD'}")

        raw_eq = nfc_rel.encode("utf-8") == nfd_rel.encode("utf-8")
        print(f"In-memory raw byte equality: {raw_eq}")
        print(f"  NFC UTF-8 bytes: {nfc_rel.encode('utf-8').hex()} ({len(nfc_rel.encode('utf-8'))} bytes)")
        print(f"  NFD UTF-8 bytes: {nfd_rel.encode('utf-8').hex()} ({len(nfd_rel.encode('utf-8'))} bytes)")
        check(nfd_accessible, "APFS did not match the NFD spelling of an NFC file")
        check(is_nfc, "APFS did not preserve the NFC bytes")
        check(not raw_eq, "NFC and NFD byte strings are equal")


if __name__ == "__main__":
    test_pipeline()
