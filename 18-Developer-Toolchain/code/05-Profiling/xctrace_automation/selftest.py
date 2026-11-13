"""Silent fault injection for trace_xml.py: prints nothing, exits non-zero if a defence stops firing.

Each case is a small hand-written export that breaks one rule of the format. A reader that
accepted any of them would turn a truncated or changed export into wrong numbers instead of an error.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from trace_xml import Export, TraceXMLError, integer

SCHEMA = (
    '<schema name="time-profile">'
    "<col><mnemonic>time</mnemonic><name>Sample Time</name><engineering-type>sample-time</engineering-type></col>"
    "<col><mnemonic>weight</mnemonic><name>Weight</name><engineering-type>weight</engineering-type></col>"
    "<col><mnemonic>stack</mnemonic><name>Backtrace</name><engineering-type>tagged-backtrace</engineering-type></col>"
    "</schema>"
)
STACK = (
    '<tagged-backtrace id="3"><frame id="4" name="leaf" addr="0x1"/>'
    '<frame id="5" name="root" addr="0x2"/></tagged-backtrace>'
)
ROW1 = f'<row><sample-time id="1">10</sample-time><weight id="2">1000000</weight>{STACK}</row>'


def document(*nodes: str, root: str = "trace-query-result") -> str:
    return f'<?xml version="1.0"?><{root}>' + "".join(f"<node>{n}</node>" for n in nodes) + f"</{root}>"


def parse(xml: str, directory: Path) -> Export:
    path = directory / "case.xml"
    path.write_text(xml)
    return Export(path)


def expect(ok: bool, what: str) -> None:
    if not ok:
        raise SystemExit(f"selftest: {what}")


def must_fail(xml: str, needle: str, directory: Path) -> None:
    try:
        parse(xml, directory)
    except TraceXMLError as err:
        if needle not in str(err):
            raise SystemExit(f"selftest: wrong error, wanted {needle!r}, got {err}") from None
        return
    raise SystemExit(f"selftest: accepted a document that should fail with {needle!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)

        # The well-formed case: the second row is refs only, and one frame is a ref inside a new stack.
        row2 = '<row><sample-time id="6">20</sample-time><weight ref="2"/><tagged-backtrace ref="3"/></row>'
        row3 = (
            '<row><sample-time id="7">30</sample-time><weight ref="2"/>'
            '<tagged-backtrace id="8"><frame id="9" name="other" addr="0x3"/><frame ref="5"/></tagged-backtrace></row>'
        )
        row4 = '<row><sample-time id="10">40</sample-time><weight ref="2"/><sentinel/></row>'
        good = parse(document(SCHEMA + ROW1 + row2 + row3 + row4, row2.replace('id="6"', 'id="11"')), directory)
        first, second = good.tables
        stacks = [good.frames(r["stack"]) for r in first.rows]
        expect(stacks == [("leaf", "root"), ("leaf", "root"), ("other", "root"), ()], f"wrong stacks: {stacks}")
        expect([integer(r["weight"]) for r in first.rows] == [1000000] * 4, "a weight ref did not resolve")
        expect(first.ref_cells[0] == frozenset() and "stack" in first.ref_cells[1], "ref cells not recorded")
        # A node without its own <schema> inherits the previous one, and ids resolve across nodes.
        expect(second.schema == "time-profile", "second node did not inherit the schema")
        expect(good.frames(second.rows[0]["stack"]) == ("leaf", "root"), "a ref did not resolve across nodes")

        must_fail(document(SCHEMA + ROW1, root="trace-toc"), "expected <trace-query-result>", directory)
        must_fail(document(SCHEMA + ROW1 + ROW1), "defined twice", directory)
        dangling = '<row><sample-time id="6">20</sample-time><weight ref="2"/><tagged-backtrace ref="99"/></row>'
        must_fail(document(SCHEMA + ROW1 + dangling), "points at no id", directory)
        wrong_tag = '<row><sample-time id="6">20</sample-time><weight ref="3"/><tagged-backtrace ref="3"/></row>'
        must_fail(document(SCHEMA + ROW1 + wrong_tag), "resolves to <tagged-backtrace>", directory)
        short = '<row><sample-time id="6">20</sample-time><weight ref="2"/></row>'
        must_fail(document(SCHEMA + ROW1 + short), "2 cells for 3 columns", directory)
        must_fail(document(ROW1), "first <node> has no <schema>", directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
