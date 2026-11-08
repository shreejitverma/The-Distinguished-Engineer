"""Print a wheel's tags, its WHEEL metadata, the extension filename, and whether macOS 15 on arm64 would select it.

Usage: inspect_wheel.py <wheel> <extract-dir>
"""

import sys
import zipfile
from pathlib import Path

import packaging.tags
from packaging.utils import parse_wheel_filename


def accepted_on_older_macos(tags: set[packaging.tags.Tag]) -> bool:
    """True if CPython 3.14 on macOS 15 arm64 would select a wheel with any of these tags.

    Comparing against the build host's own sys_tags() proves nothing, because a host always accepts what it built.
    A fixed older OS makes the check discriminate: a macosx_27_0 wheel is absent from its list and a macosx_11_0 wheel is present.
    """
    platforms = list(packaging.tags.mac_platforms((15, 0), "arm64"))
    older = set(packaging.tags.cpython_tags(python_version=(3, 14), abis=["cp314"], platforms=platforms))
    return any(t in older for t in tags)


def main(wheel: Path, extract_dir: Path) -> None:
    _, _, _, tags = parse_wheel_filename(wheel.name)
    names = sorted(str(t) for t in tags)
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(extract_dir)
        wheel_meta = next(n for n in zf.namelist() if n.endswith(".dist-info/WHEEL"))
        meta_tags = sorted(line.split(":", 1)[1].strip()
                           for line in zf.read(wheel_meta).decode().splitlines() if line.startswith("Tag:"))
        modules = sorted(n for n in zf.namelist() if n.endswith(".so"))
    print(f"Filename tags: {', '.join(names)}")
    print(f"WHEEL Tag lines: {', '.join(meta_tags)}")
    print(f"Extension module file: {', '.join(Path(m).name for m in modules)}")
    print(f"Selected by CPython 3.14 on macOS 15 arm64: {'yes' if accepted_on_older_macos(set(tags)) else 'no'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
