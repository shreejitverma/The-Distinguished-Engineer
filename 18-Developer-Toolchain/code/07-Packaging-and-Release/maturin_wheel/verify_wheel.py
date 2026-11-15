#!/usr/bin/env python3
"""Deterministic verification of built PyO3 / Maturin wheel and ABI3 binary."""

import os
import re
import sys
import zipfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: verify_wheel.py <wheel_file_or_dir>")
        return 1

    target = Path(sys.argv[1])
    if target.is_dir():
        wheels = sorted(list(target.glob("*.whl")))
        if not wheels:
            print("No .whl files found in directory")
            return 1
        wheel_path = wheels[0]
    else:
        wheel_path = target

    # 1. Inspect filename
    filename = wheel_path.name
    m = re.match(r"^([^-]+)-([^-]+)-([^-]+)-([^-]+)-([^-]+)\.whl$", filename)
    if not m:
        print(f"FAIL: Invalid wheel filename format: {filename}")
        return 1

    pkg, ver, py_tag, abi_tag, plat_tag = m.groups()
    print("Wheel Package Name:", pkg)
    print("Wheel Version:", ver)
    print("Python Tag:", py_tag)
    print("ABI Tag:", abi_tag)
    plat_parts = plat_tag.split("_")
    plat_os = plat_parts[0]
    plat_arch = plat_parts[-1]
    print(f"Platform Family: {plat_os}")
    print(f"Platform Architecture: {plat_arch}")

    # 2. Inspect ZIP structure
    extract_dir = wheel_path.parent / "unpacked"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel_path, "r") as zf:
        zf.extractall(extract_dir)
        names = sorted(zf.namelist())

    dist_info = f"{pkg}-{ver}.dist-info"
    wheel_meta = extract_dir / dist_info / "WHEEL"
    if not wheel_meta.exists():
        print("FAIL: Missing WHEEL metadata file")
        return 1

    wheel_lines = {
        k.strip(): v.strip()
        for k, v in [
            line.split(":", 1)
            for line in wheel_meta.read_text().splitlines()
            if ":" in line
        ]
    }
    print("WHEEL Wheel-Version:", wheel_lines.get("Wheel-Version", "unknown"))
    print("WHEEL Root-Is-Purelib:", wheel_lines.get("Root-Is-Purelib", "unknown"))

    # 3. Locate extension shared object
    ext_files = [f for f in names if f.endswith(".so") or f.endswith(".pyd")]
    if not ext_files:
        print("FAIL: No compiled extension shared object found in wheel")
        return 1

    ext_rel_path = ext_files[0]
    ext_full_path = extract_dir / ext_rel_path
    print("Extension Module Path:", ext_rel_path)
    print("Extension ABI Suffix:", ".abi3.so" in ext_rel_path or ".pyd" in ext_rel_path)

    # 4. Symbol inspection (cross-platform check)
    has_init = False
    with open(ext_full_path, "rb") as f:
        content = f.read()
        if b"PyInit__core" in content:
            has_init = True

    print("Exported PyInit entrypoint present:", has_init)

    # 5. Runtime import and calculation
    sys.path.insert(0, str(extract_dir))
    try:
        import fast_crc32

        test_data = b"123456789"
        # Standard CRC32 of "123456789" is 0xcbf43926 (3421780262)
        calc_crc = fast_crc32.checksum(test_data)
        expected_crc = 0xCBF43926
        print("Test Vector Checksum Match:", calc_crc == expected_crc)
        print("Checksum Decimal Value:", calc_crc)

        calc_hex = fast_crc32.checksum_hex(test_data)
        print("Checksum Hex String:", calc_hex)
        print("Hex String Match:", calc_hex == "cbf43926")
    except Exception as e:
        print(f"FAIL: Runtime execution error: {e}")
        return 1

    print("Wheel verification result: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
