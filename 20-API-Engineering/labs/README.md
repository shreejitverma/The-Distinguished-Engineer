---
type: moc
track: [sde, distinguished, ai-eng]
level:
status: draft
last_reviewed:
sources: []
---

# API Engineering Labs and Verification Harness

Every executable claim in [API Engineering](../README.md) is backed by a deterministic, self-contained lab in this workspace and gated in CI by `python3 tools/check_api_labs.py`.
All notes share a single `uv`-locked workspace (`uv 0.11.26`, `.python-version` `3.14`) so that every lab runs against one hermetic `uv.lock` file without duplicating virtual environments per note.
SQLAlchemy is pinned twice as conflicting dependency groups resolved in the same `uv.lock`: `sqlalchemy21` (`2.1.8`) is a default group, and `sqlalchemy20` (`2.0.57`) is the deliberate 2.0 compatibility leg for labs that compare the two series.
See [STYLE.md](../STYLE.md) for the note contract that binds prose claims to these labs.

## Workspace layout

```text
20-API-Engineering/labs/
  README.md
  pyproject.toml
  uv.lock
  .python-version
  src/
    api_labs/
      __init__.py
      f01_http_semantics_and_wire_protocols/
      ...
      f27_reference/
  tests/
    conftest.py
    f01_http_semantics_and_wire_protocols/
    ...
    f27_reference/
```

Container definitions (`Dockerfile.debian`, `Dockerfile.alpine`, and `compose.yaml`) are not checked in yet; they arrive with the first lab that needs a real PostgreSQL, Valkey, or Envoy service, together with its pytest marker and gate option.
Optional contract fixtures live under `fixtures/openapi/` and `fixtures/proto/` once contract labs are added.

## Naming and traceability rules

Every folder `NN-<Folder-Name>` in `20-API-Engineering/` maps to a package `fNN_<slug>` under both `src/api_labs/` and `tests/`, where `<slug>` is the folder name after `NN-`, lowercased with hyphens replaced by underscores.
For example, `01-HTTP-Semantics-and-Wire-Protocols` maps to `f01_http_semantics_and_wire_protocols`.
Each note's pytest module is named `test_<stem_snake>.py` inside its folder's `tests/fNN_<slug>/` package, where `<stem_snake>` is the note file stem lowercased with every non-alphanumeric run replaced by `_`.
For example, the note `HTTP-1-1-Framing-Keep-Alive-and-Chunked-Encoding.md` maps to `tests/f01_http_semantics_and_wire_protocols/test_http_1_1_framing_keep_alive_and_chunked_encoding.py`.

A note declares its lab by adding a bullet under its `## Related` section in the exact form below:

```markdown
- Lab: `labs/tests/f01_http_semantics_and_wire_protocols/test_http_1_1_framing_keep_alive_and_chunked_encoding.py`
```

A note declares at most one lab, because the only valid path is the one derived from its folder and stem, and the declared path must be tracked in git.
Every tracked `labs/tests/**/test_*.py` file must be declared by exactly one note, with no orphan test modules and no double owners.
Once a note moves past `status: seed` to `draft`, `solid`, or `canonical`, it must replace its `Lab plan:` placeholder line with a real `- Lab:` declaration or remove the plan line if no lab applies.

## Execution tiers and pytest markers

Every lab test carries exactly one tier marker; `tests/conftest.py` fails collection (pytest exit code 4) for a test with none or both, so no lab is silently deselected.

1. `@pytest.mark.unit` (Tier 1): Runs entirely in-process without opening network sockets or talking to external daemons.
   ASGI and FastAPI endpoints use `httpx.AsyncClient` with `httpx.ASGITransport` and `asgi_lifespan.LifespanManager`, wire framing labs feed raw byte buffers into `h11`, `h2`, or `aioquic` state machines, Redis rate limiters use `fakeredis[lua]`, and SQLite labs use `aiosqlite`.
2. `@pytest.mark.socket` (Tier 2): Binds ephemeral loopback sockets (`127.0.0.1:0`) or UNIX domain sockets for TLS 1.3 and mTLS handshakes, ASGI server worker lifecycle and `SIGTERM` drain tests, and `grpc.aio` streaming and deadline propagation.

## Running locally

Run the full gate from the repository root:

```sh
python3 tools/check_api_labs.py
python3 tools/check_api_labs.py --only traceability --only ascii
```

Or run individual tools directly inside `20-API-Engineering/labs/`:

```sh
cd 20-API-Engineering/labs
uv lock --check
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen mypy src tests
uv run --frozen pyright src tests
uv run --frozen pytest --cov --cov-report=term-missing --cov-fail-under=95
```

Run the suite against the SQLAlchemy 2.0 compatibility leg instead of the default 2.1 group:

```sh
uv run --frozen --no-group sqlalchemy21 --group sqlalchemy20 pytest
```
