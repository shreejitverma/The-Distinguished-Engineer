"""Parser for the Makefile subset that GCC and Clang write with -MD/-MMD -MF.

The escaping rules follow the comment above DepfileParser::Parse in Ninja's
src/depfile_parser.in.cc, which documents what GCC and Clang actually emit:

- Backslash-newline is a line continuation and ends the current file name.
- A space preceded by 2N+1 backslashes is N backslashes followed by a space
  that belongs to the file name.
- A space preceded by 2N backslashes is 2N backslashes at the end of a file
  name, and the space separates names.
- A hash sign escaped by a single backslash is a literal '#'.
- '$$' is a literal '$'.
- All other backslashes are kept unchanged.

A file name that ends in ':' (followed by whitespace or end of input) is a
target; names after it on the same logical line are its prerequisites.
A rule with no prerequisites, such as the phony 'hdr.h:' lines that -MP adds,
is returned like any other rule; the caller decides what to ignore.
"""

from __future__ import annotations


class DepfileError(ValueError):
    pass


def parse(text: str) -> list[tuple[list[str], list[str]]]:
    """Return the rules in order as (targets, prerequisites) pairs."""
    rules: list[tuple[list[str], list[str]]] = []
    targets: list[str] = []
    deps: list[str] = []
    in_deps = False
    tok: list[str] = []
    saw_content = False

    def end_token() -> None:
        nonlocal in_deps, saw_content
        if not tok:
            return
        word = "".join(tok)
        tok.clear()
        saw_content = True
        if not in_deps and word.endswith(":"):
            if word[:-1]:
                targets.append(word[:-1])
            in_deps = True
        elif in_deps:
            deps.append(word)
        else:
            targets.append(word)

    def end_rule() -> None:
        nonlocal targets, deps, in_deps
        end_token()
        if targets or deps:
            if not in_deps:
                raise DepfileError(f"expected ':' in depfile after {targets!r}")
            rules.append((targets, deps))
        targets, deps, in_deps = [], [], False

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            j = i
            while j < n and text[j] == "\\":
                j += 1
            run = j - i
            nxt = text[j] if j < n else ""
            if nxt == " ":
                if run % 2 == 1:
                    tok.append("\\" * (run // 2) + " ")
                    i = j + 1
                else:
                    tok.append("\\" * run)
                    i = j  # the space ends the name on the next iteration
                continue
            if nxt == "#" and run == 1:
                tok.append("#")
                i = j + 1
                continue
            if run == 1 and (nxt == "\n" or text.startswith("\r\n", j)):
                end_token()  # continuation: the logical line goes on
                i = j + (2 if nxt == "\r" else 1)
                continue
            tok.append("\\" * run)
            i = j
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "$":
            tok.append("$")
            i += 2
            continue
        if c in " \t":
            end_token()
            i += 1
            continue
        if c in "\r\n":
            end_rule()
            i += 1
            continue
        tok.append(c)
        i += 1
    end_rule()
    if saw_content and not rules:
        raise DepfileError("expected ':' in depfile")
    return rules


def escape(name: str) -> str:
    """Escape one file name the way GCC writes it, for the toy compiler."""
    out = []
    for k, ch in enumerate(name):
        if ch == " ":
            # Double any backslashes that end right before the space, then escape it.
            m = 0
            while k - 1 - m >= 0 and name[k - 1 - m] == "\\":
                m += 1
            out.append("\\" * m + "\\ ")
        elif ch == "#":
            out.append("\\#")
        elif ch == "$":
            out.append("$$")
        else:
            out.append(ch)
    return "".join(out)


def prerequisites_of(text: str, target: str) -> list[str]:
    """Prerequisites of `target`, merged across rules, in first-seen order."""
    seen: dict[str, None] = {}
    found = False
    for targets, deps in parse(text):
        if target in targets:
            found = True
            for d in deps:
                seen.setdefault(d, None)
    if not found:
        raise DepfileError(f"depfile has no rule for {target!r}")
    return list(seen)
