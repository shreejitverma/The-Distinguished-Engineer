#!/usr/bin/env python3
"""Auditwheel policy inspection and repair simulator for manylinux compliance."""
import sys
import os
import re
import subprocess
import zipfile
import shutil

POLICIES = {
    "manylinux2014": (2, 17),
    "manylinux_2_17": (2, 17),
    "manylinux_2_28": (2, 28),
    "manylinux_2_31": (2, 31),
    "manylinux_2_34": (2, 34),
    "manylinux_2_35": (2, 35),
    "manylinux_2_38": (2, 38),
    "manylinux_2_39": (2, 39),
}

def parse_glibc_versions_from_elf(elf_path):
    cmd = ["readelf", "-V", elf_path]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    versions = set()
    for line in res.stdout.splitlines():
        m = re.search(r"Name:\s+GLIBC_([0-9]+\.[0-9]+)", line)
        if m:
            v_str = m.group(1)
            parts = tuple(map(int, v_str.split(".")))
            versions.add((parts, f"GLIBC_{v_str}"))
    return sorted(versions)

def cmd_show(wheel_or_so, arch):
    versions = parse_glibc_versions_from_elf(wheel_or_so)
    version_names = [v[1] for v in versions]
    max_ver = max(v[0] for v in versions) if versions else (2, 17)
    
    print(f"auditwheel show: {os.path.basename(wheel_or_so)}")
    print(f"  Found external shared library dependency: libc.so.6")
    print(f"  Required symbol versions: {', '.join(version_names)}")
    
    # Determine minimum policy
    matching_policy = None
    for p_name, p_ver in sorted(POLICIES.items(), key=lambda x: x[1]):
        if p_name.startswith("manylinux_") and max_ver <= p_ver:
            matching_policy = f"{p_name}_{arch}"
            break
    if not matching_policy:
        matching_policy = f"manylinux_{max_ver[0]}_{max_ver[1]}_{arch}"
        
    print(f'  This constrains the platform tag to "{matching_policy}".')
    if max_ver > (2, 28):
        print('  In order to achieve a more compatible tag, you would need to recompile a new wheel from source on a system with earlier versions of these libraries, such as a recent manylinux image.')

def cmd_repair(wheel_or_so, plat, out_dir):
    arch = plat.split("_")[-1]
    policy_base = plat[:-(len(arch) + 1)]
    if policy_base not in POLICIES:
        print(f"Unknown policy: {policy_base}", file=sys.stderr)
        sys.exit(1)
    
    allowed_max = POLICIES[policy_base]
    versions = parse_glibc_versions_from_elf(wheel_or_so)
    offending = [v[1] for v in versions if v[0] > allowed_max]
    
    if offending:
        print(
            f'auditwheel: error: cannot repair "{os.path.basename(wheel_or_so)}" to "{plat}" ABI '
            f'because of the presence of too-recent versioned symbols: libc.so.6 with version {offending[-1]}',
            file=sys.stderr
        )
        sys.exit(1)
        
    os.makedirs(out_dir, exist_ok=True)
    out_name = f"fast_hasher-1.0.0-cp312-cp312-{plat}.whl"
    out_path = os.path.join(out_dir, out_name)
    # Write a dummy repaired wheel
    with open(out_path, "w") as f:
        f.write("repaired wheel")
    print(f"Fixed-up wheel written to {out_path}")

def main():
    if len(sys.argv) < 3:
        print("Usage: audit_wheel.py [show <file> <arch> | repair --plat <plat> <file> <out_dir>]")
        sys.exit(1)
        
    action = sys.argv[1]
    if action == "show":
        cmd_show(sys.argv[2], sys.argv[3])
    elif action == "repair":
        if sys.argv[2] != "--plat":
            sys.exit(1)
        plat = sys.argv[3]
        target = sys.argv[4]
        out_dir = sys.argv[5]
        cmd_repair(target, plat, out_dir)

if __name__ == "__main__":
    main()
