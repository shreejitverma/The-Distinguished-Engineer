"""Write the toy registry as metadata-only wheels, so uv resolves the same graph offline.

Usage: python3 make_wheels.py OUT_DIR

Each wheel holds only a dist-info directory (METADATA, WHEEL, RECORD); a resolver
reads Requires-Dist from METADATA and never needs any code. Zip entries carry a
fixed timestamp so two runs produce byte-identical files.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import zipfile
from pathlib import Path

from registry import REGISTRY

FIXED_DATE = (2026, 11, 1, 0, 0, 0)


def requires_dist(name: str, spec: str, platform: str | None) -> str:
    line = f"Requires-Dist: {name}{spec}"
    if platform is not None:
        line += f'; sys_platform == "{platform}"'
    return line


def record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(data)}"


def build_wheel(out: Path, name: str, version: str, deps: list[tuple[str, str, str | None]]) -> Path:
    dist = name.replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    metadata = "\n".join(
        ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
        + [requires_dist(*dep) for dep in deps]
    ) + "\n"
    wheel = "Wheel-Version: 1.0\nGenerator: pubgrub-drill\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files = {f"{info}/METADATA": metadata.encode(), f"{info}/WHEEL": wheel.encode()}
    record = "\n".join(record_line(p, d) for p, d in files.items()) + f"\n{info}/RECORD,,\n"
    files[f"{info}/RECORD"] = record.encode()
    path = out / f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, data in files.items():
            zf.writestr(zipfile.ZipInfo(member, FIXED_DATE), data)
    return path


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for name, versions in REGISTRY.items():
        for version, deps in versions.items():
            build_wheel(out, name, version, deps)
            count += 1
    print(f"wrote {count} metadata-only wheels for {len(REGISTRY)} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
