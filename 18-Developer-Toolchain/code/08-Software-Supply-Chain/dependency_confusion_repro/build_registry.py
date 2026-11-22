"""Build two tiny mock package registries for the dependency confusion lab.

Both registries speak the PEP 503 / Simple Repository API HTML format well
enough for pip and uv to resolve against, and both serve a real wheel for a
project named acme-internal-widgets:
  - the "internal" registry serves version 1.0.0, a benign package.
  - the "public_mirror" registry serves version 9.9.9, a package whose
    __init__.py writes a marker file the instant it is imported, standing
    in for the torchtriton payload (PyTorch Foundation, "Compromised
    PyTorch-nightly Dependency Chain", 2022) that ran on import rather than
    on install.
Everything here is hand-built with the standard library only: no setuptools
or build backend is invoked to produce the wheels, and no package code
touches the network. The marker payload only writes a file inside the
directory named by the DC_LAB_MARKER_DIR environment variable, so the lab
never leaves a trace outside its own scratch directory.

Wheel layout follows the Binary distribution format specification
(packaging.python.org/specifications/binary-distribution-format), the
RECORD format follows the Recording installed packages specification
(.../specifications/recording-installed-packages), and the per-project
index page follows PEP 503 / the Simple repository API specification.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import zipfile

DIST = "acme_internal_widgets"           # wheel/dist-info name (PEP 427 escaping: - -> _)
PROJECT = "acme-internal-widgets"         # PyPI-style project name used in --index-url lookups


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _wheel_metadata(version: str, summary: str) -> bytes:
    return (
        "Metadata-Version: 2.1\n"
        f"Name: {PROJECT}\n"
        f"Version: {version}\n"
        f"Summary: {summary}\n"
        "Requires-Python: >=3.9\n"
    ).encode("ascii")


def _wheel_wheel_file() -> bytes:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: dependency_confusion_repro 1.0\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("ascii")


def build_wheel(out_dir: str, version: str, init_py: str, summary: str) -> str:
    dist_info = f"{DIST}-{version}.dist-info"
    metadata = _wheel_metadata(version, summary)
    wheel_file = _wheel_wheel_file()
    init_bytes = init_py.encode("ascii")

    files: list[tuple[str, bytes]] = [
        (f"{DIST}/__init__.py", init_bytes),
        (f"{dist_info}/METADATA", metadata),
        (f"{dist_info}/WHEEL", wheel_file),
    ]
    # Per the recording-installed-packages spec, RECORD lists every installed
    # file including itself, and the RECORD entry for itself carries empty
    # hash and size fields.
    record_lines = [f"{path},{_record_hash(data)},{len(data)}" for path, data in files]
    record_lines.append(f"{dist_info}/RECORD,,")
    record_bytes = ("\n".join(record_lines) + "\n").encode("ascii")

    wheel_name = f"{DIST}-{version}-py3-none-any.whl"
    wheel_path = os.path.join(out_dir, wheel_name)
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files:
            zf.writestr(path, data)
        zf.writestr(f"{dist_info}/RECORD", record_bytes)
    return wheel_path


def write_simple_index(registry_root: str, wheel_filename: str, wheel_sha256: str) -> None:
    # PEP 503: the project page is a valid HTML5 page with one anchor per
    # file, href pointing at the file, anchor text equal to the filename,
    # and an optional #sha256=... fragment.
    project_dir = os.path.join(registry_root, "simple", PROJECT)
    os.makedirs(project_dir, exist_ok=True)
    html = (
        "<!DOCTYPE html>\n<html>\n<body>\n"
        f'<a href="{wheel_filename}#sha256={wheel_sha256}">{wheel_filename}</a>\n'
        "</body>\n</html>\n"
    )
    with open(os.path.join(project_dir, "index.html"), "w", encoding="ascii") as fh:
        fh.write(html)


def main(root: str) -> None:
    internal_root = os.path.join(root, "internal")
    public_root = os.path.join(root, "public_mirror")
    os.makedirs(os.path.join(internal_root, "simple", PROJECT), exist_ok=True)
    os.makedirs(os.path.join(public_root, "simple", PROJECT), exist_ok=True)

    benign_init = (
        "import os\n"
        "_d = os.environ.get('DC_LAB_MARKER_DIR')\n"
        "if _d:\n"
        "    with open(os.path.join(_d, 'INTERNAL_IMPORT_OK'), 'w', encoding='ascii') as _f:\n"
        "        _f.write('internal 1.0.0 imported\\n')\n"
    )
    wheel_path = build_wheel(
        os.path.join(internal_root, "simple", PROJECT),
        "1.0.0",
        benign_init,
        "Internal widget helpers (private index only).",
    )
    sha = hashlib.sha256(open(wheel_path, "rb").read()).hexdigest()
    write_simple_index(internal_root, os.path.basename(wheel_path), sha)

    malicious_init = (
        "import os\n"
        "_d = os.environ.get('DC_LAB_MARKER_DIR')\n"
        "if _d:\n"
        "    with open(os.path.join(_d, 'DEPENDENCY_CONFUSION_RCE_FIRED'), 'w', encoding='ascii') as _f:\n"
        "        _f.write('payload ran on import of acme_internal_widgets 9.9.9\\n')\n"
    )
    wheel_path = build_wheel(
        os.path.join(public_root, "simple", PROJECT),
        "9.9.9",
        malicious_init,
        # Deliberately imitates the internal package's summary: the attacker
        # has no reason to make the typosquat/confusable listing look
        # suspicious.
        "Internal widget helpers.",
    )
    sha = hashlib.sha256(open(wheel_path, "rb").read()).hexdigest()
    write_simple_index(public_root, os.path.basename(wheel_path), sha)


if __name__ == "__main__":
    main(sys.argv[1])
