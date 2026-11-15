"""The toy registry for the PubGrub diamond drill.

One source of truth: pubgrub.py solves it directly, and make_wheels.py turns
it into metadata-only wheels so the real uv resolver sees the same graph.

The diamond:

    svc (root) --> ingest >=2.0.0 ----------------------------> wire >=3.0.0
               +-> report >=1.0.0 --(linux only)--> wire-native <2.0.0 --> wire <3.0.0

A dependency is (name, specifier, platform) where platform is None for an
unconditional requirement or a sys_platform value such as "linux", which
becomes the PEP 508 marker `sys_platform == "linux"` in the wheel metadata.
"""

from __future__ import annotations

ROOT = "svc"

# The project's own requirements (what pyproject.toml or requirements.in says).
ROOT_DEPS = [
    ("ingest", ">=2.0.0", None),
    ("report", ">=1.0.0", None),
]

_REPORT_1X = [
    ("wire", ">=2.0.0", None),
    ("wire-native", ">=1.0.0,<2.0.0", "linux"),
]

REGISTRY: dict[str, dict[str, list[tuple[str, str, str | None]]]] = {
    "ingest": {
        "1.4.0": [("wire", ">=2.0.0,<3.0.0", None)],
        "2.0.0": [("wire", ">=3.0.0", None)],
        "2.1.0": [("wire", ">=3.0.0", None)],
    },
    "report": {
        # 0.9.0 predates the native accelerator, so it has no wire-native edge.
        "0.9.0": [("wire", ">=2.0.0", None)],
        "1.0.0": list(_REPORT_1X),
        "1.1.0": list(_REPORT_1X),
        "1.2.0": list(_REPORT_1X),
    },
    "wire-native": {
        "1.0.0": [("wire", ">=2.0.0,<3.0.0", None)],
        "1.1.0": [("wire", ">=2.0.0,<3.0.0", None)],
        # 2.0.0 was rebuilt against the wire 3 ABI.
        "2.0.0": [("wire", ">=3.0.0", None)],
    },
    "wire": {
        "2.0.0": [],
        "2.1.0": [],
        "3.0.0": [],
        "3.1.0": [],
    },
}

PLATFORMS = ("darwin", "linux", "win32")
