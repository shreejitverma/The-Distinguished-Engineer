---
type: concept
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# Note Contract for API Engineering

These rules govern every note in `20-API-Engineering/`.
They extend the vault conventions in the repository `CLAUDE.md` and `00-Start-Here/How-This-Vault-Works.md`, adapted to APIs, HTTP, and the Python API stack whose claims can be executed.

## Shape of the knowledge base

One concept per note.
Split a note when its core idea needs more than one diagram.
File names are hyphenated, for example `HTTP-Message-Abstraction-RFC-9110.md`, and unique across the whole vault, so a bare wikilink resolves.
The H1 is the human title; the file stem never changes once other notes link to it.
Each folder README is a map of content with the folder's scope and a numbered Learning order, which is the canonical reading sequence.

## Prose and formatting

One full sentence per physical line.
No emojis anywhere, and no em dashes or en dashes; use a plain dash "-".
ASCII only in notes and labs; write arrows as "->" and comparisons as ">=".
Every piece of code, HTTP method, header name, status code, JSON field, and CLI syntax in prose goes in backticks, including `GET`, `Content-Type`, `200 OK`, `{"id": 1}`, and `uv run pytest`.
Inside a Markdown table, write a wikilink alias as `[[Note\|Alias]]`.
In Mermaid labels, write a line break as `<br/>`; Mermaid prints `\n` literally.
Dense, factual prose; mechanism first; name the trade-off and the failure mode of every recommendation.

## The solid bar

A note reaches `status: solid` when it has these sections, in this order:

1. `# Title`, then a `> [!summary] Claim` callout with the one sentence a tired reader needs.
2. `## TL;DR`: five lines covering the mechanism, the contract rule, the trap, the portability fact, and when not to use it.
3. `## Mental model`: one Mermaid diagram (a flowchart, `stateDiagram-v2`, or `sequenceDiagram`) and one paragraph narrating its arrows.
4. `## How it works`: the mechanism down to the wire byte layout, the specification section, and the source implementation.
   Cite RFCs by section, for example `(RFC 9110 Section 9.2.2)`, library source by release, file path, and function or struct, for example `(pydantic-core 2.50.4 src/validators/model.rs, validate)`, and server internals by source path or protocol specification, each checked against the real source code.
5. `## Numbers and limits`: a table; every number is sourced and dated, or measured in the lab with the command, machine, operating system, and runtime version stated.
6. `## Portability`: a table across the CPython version matrix (`3.12.16`, `3.13.18`, `3.14.10`, `3.15.2`, and `3.15.2t` free-threaded), library point releases, server implementations (Uvicorn, Hypercorn, Granian), and HTTP versions (HTTP/1.1, HTTP/2, HTTP/3 over QUIC); every cell is proven by the lab matrix or cites an authoritative specification or upstream issue.
7. `## Worked example`: links to the note's runnable implementation under `labs/src/api_labs/` and its pytest test under `labs/tests/`, the real code, and output captured from a real execution run.
8. `## Pitfalls`: `> [!warning]` callouts with trigger, symptom, and fix, citing real production incidents, CVEs, and postmortems by primary source.
9. `## Security and robustness`: threat models, OWASP API Security risks, input validation edge cases, boundary enforcement, and defense in depth.
10. `## The principal's view`: the architectural decision a staff-plus engineer makes about this topic, its organizational trade-off, and when to choose a different protocol, paradigm, or language.
11. `## Interview questions`: at least eight across L5, L6, and L7+, each a `> [!question]` callout with a folded `> > [!success]- Answer` and a follow-up, including one predict-the-output question and one debug-this-API question.
12. `## Related`: `Prerequisites:`, `Next:`, and `See also:` lines of wikilinks, inside this section and across the vault.
13. `## Further reading`: primary sources only (RFCs, official specification documents, PEPs, upstream source commits, and author papers), one line each on why it matters.

Notes in `26-Capstone-Projects` and `27-Reference` keep sections 1, 2, 12, and 13 and adapt the rest to their form (an architectural specification and acceptance test plan, or a reference table).

## Verification

API and runtime behavior is checkable, so check it: run it in the pytest matrix, inspect raw bytes in wire state machines, and read the specification and source code.
Every executable claim is backed by a deterministic lab in `labs/` and gated in CI by `python3 tools/check_api_labs.py`.
The lab harness is a single uv-locked workspace where every note maps to one pytest test module under `labs/tests/fNN_<slug>/test_<stem>.py`.
Labs are deterministic and hermetic: every test must be able to fail when the implementation or invariant is broken, and cannot rely on unpinned state or external internet access.
The worked example in every note is real, runnable lab code from `labs/src/api_labs/`, not pseudocode or unverified snippets.
Claims are proven across the pinned CPython matrix (3.12.16, 3.13.18, 3.14.10, 3.15.2, and 3.15.2t free-threaded) and container environments (Debian Trixie and Alpine musl libc).
Never invent an HTTP header, status code, RFC section, configuration flag, source function, CVE, library release, benchmark result, or citation.
Cite RFC sections explicitly, for example `(RFC 9110 Section 9.2.2)`.
Cite library implementation source as `(<package> <version> <path>, <symbol>)`, for example `(pydantic-core 2.50.4 src/validators/model.rs, validate)`, only after reading the actual source file in that release.
State the exact versions of every behavior that varies between library or runtime releases, and date volatile facts: `(as of 2026-11)`.
A claim that cannot be verified is omitted or marked `UNVERIFIED: <why>`.
Source priority: IETF RFCs and BCPs, official specification standards, library source code and maintainer documentation, PEPs, peer-reviewed systems papers, then reputable primary postmortems.

## Lifecycle

`seed`: frontmatter, the claim placeholder, empty section headings, and Related links.
`draft`: every section written and the lab passing; `UNVERIFIED` markers allowed.
`solid`: an independent fact-check by a reviewer who did not draft the note has rerun the lab, re-executed every portability claim, rechecked every number and citation, and left no `UNVERIFIED` marker.
`canonical`, `level`, and `last_reviewed` are set only by a human.
Every primary source a note relies on is listed in its `sources:` frontmatter.

## Later: the book

These notes are the source for a future Zero to Godhood book, compiled separately outside this vault.
Keep that compile mechanical: folder order is part order, each folder's Learning order is chapter order, notes stay self-contained with stable headings, and labs are referenced by path.
