"""Read the XML that `xctrace export` writes.

The export is a `<trace-query-result>` holding one `<node>` per table the XPath matched.
A node carries a `<schema>` (one `<col>` per column, each with a `<mnemonic>`) and then `<row>`
elements whose children are the column values in schema order; an empty cell is `<sentinel/>`.

The export deduplicates values. The first time a value appears it carries `id="N"`; every later
use is an empty element of the same tag with `ref="N"`. Ids are unique across the whole
document, not per node and not per row, and a value can be referenced at any depth: a whole
backtrace, one frame inside a backtrace, or the binary inside a frame. A reader that ignores
`ref` sees only the first occurrence of each stack and under-counts everything else.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


class TraceXMLError(Exception):
    """The export does not have the shape this reader understands."""


@dataclass(frozen=True)
class Column:
    mnemonic: str
    name: str
    engineering_type: str


@dataclass
class Table:
    schema: str
    columns: list[Column]
    rows: list[dict[str, ET.Element]] = field(default_factory=list)
    # For each row, the mnemonics whose cell was written as a `ref`, before resolution.
    ref_cells: list[frozenset[str]] = field(default_factory=list)

    def column(self, mnemonic: str) -> Column:
        for col in self.columns:
            if col.mnemonic == mnemonic:
                return col
        raise TraceXMLError(f"table {self.schema} has no column {mnemonic!r}")


class Export:
    """One parsed `xctrace export --xpath` document with every `ref` resolvable."""

    def __init__(self, path: Path) -> None:
        self.root = ET.parse(path).getroot()
        if self.root.tag != "trace-query-result":
            raise TraceXMLError(f"{path.name}: root is <{self.root.tag}>, expected <trace-query-result>")
        self.by_id: dict[str, ET.Element] = {}
        for elem in self.root.iter():
            ident = elem.get("id")
            if ident is None:
                continue
            if ident in self.by_id:
                raise TraceXMLError(f"{path.name}: id {ident} is defined twice")
            self.by_id[ident] = elem
        self.tables = self._read_tables(path.name)

    def deref(self, elem: ET.Element) -> ET.Element:
        """Return the defining element for a `ref`, or the element itself."""
        ref = elem.get("ref")
        if ref is None:
            return elem
        try:
            target = self.by_id[ref]
        except KeyError:
            raise TraceXMLError(f"<{elem.tag} ref={ref!r}> points at no id") from None
        if target.tag != elem.tag:
            raise TraceXMLError(f"<{elem.tag} ref={ref!r}> resolves to <{target.tag}>")
        return target

    def _read_tables(self, name: str) -> list[Table]:
        tables: list[Table] = []
        last_columns: dict[str, list[Column]] = {}
        previous: Table | None = None
        for node in self.root.findall("node"):
            schema = node.find("schema")
            if schema is not None:
                columns = [
                    Column(
                        mnemonic=col.findtext("mnemonic", ""),
                        name=col.findtext("name", ""),
                        engineering_type=col.findtext("engineering-type", ""),
                    )
                    for col in schema.findall("col")
                ]
                schema_name = schema.get("name", "")
                last_columns[schema_name] = columns
            elif previous is not None:
                # A second table of a schema already printed repeats only its rows.
                schema_name, columns = previous.schema, last_columns[previous.schema]
            else:
                raise TraceXMLError(f"{name}: first <node> has no <schema>")
            table = Table(schema=schema_name, columns=columns)
            for row in node.findall("row"):
                cells = list(row)
                if len(cells) != len(columns):
                    raise TraceXMLError(
                        f"{name}: a {schema_name} row has {len(cells)} cells for {len(columns)} columns"
                    )
                pairs = list(zip(columns, cells, strict=True))
                table.ref_cells.append(frozenset(col.mnemonic for col, cell in pairs if cell.get("ref") is not None))
                table.rows.append({col.mnemonic: self.deref(cell) for col, cell in pairs})
            tables.append(table)
            previous = table
        return tables

    def frames(self, backtrace: ET.Element) -> tuple[str, ...]:
        """Symbol names of a backtrace cell, leaf first, with frame-level refs resolved."""
        names = []
        for child in self.deref(backtrace).iter("frame"):
            frame = self.deref(child)
            names.append(frame.get("name") or frame.get("addr") or "?")
        return tuple(names)


def integer(cell: ET.Element) -> int:
    """The raw value of a numeric cell; the `fmt` attribute is only the display string."""
    if cell.tag == "sentinel" or cell.text is None:
        raise TraceXMLError(f"<{cell.tag}> holds no number")
    return int(cell.text)


def text(cell: ET.Element) -> str:
    return "" if cell.tag == "sentinel" else (cell.text or "")
