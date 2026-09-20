"""Pull a rectangular table out of an HTML page.

Real portals do not ship `data-col` attributes. The contracted *header text*
is the thing we notice changing overnight. `data-col` is an optional gift
from the bundled demo, used only to keep field names stable across a rename.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any


_NUM = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def infer_type(value: str) -> str:
    text = value.strip()
    if not text:
        return "string"
    if _DATE.match(text):
        return "date"
    if _NUM.match(text):
        return "number"
    return "string"


def coerce(value: str, typ: str) -> Any:
    text = value.strip()
    if typ == "number":
        return float(text.replace(",", ""))
    return text


def slug(header: str) -> str:
    s = header.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "field"


@dataclass
class Column:
    name: str
    header: str
    required: bool = True
    type: str = "string"
    sample: str = ""


@dataclass
class ExtractedTable:
    selector: str
    marker: str
    columns: list[Column] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    raw_rows: list[list[str]] = field(default_factory=list)

    def header_contract(self) -> list[str]:
        return [c.header for c in self.columns]

    def field_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class _Cell:
    tag: str
    name: str
    text: str


class _TableParser(HTMLParser):
    def __init__(self, want: str | None = None):
        super().__init__(convert_charrefs=True)
        self.want = want
        self.tables: list[dict[str, Any]] = []
        self._depth = 0
        self._active = False
        self._current: dict[str, Any] | None = None
        self._row: list[_Cell] | None = None
        self._cell_tag = ""
        self._cell_name = ""
        self._cell_text = ""
        self._capturing = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v or "") for k, v in attrs}
        if tag == "table":
            self._depth += 1
            if self._depth != 1 or self._active:
                return
            marker = ad.get("data-portalc", "")
            if self.want is not None and marker != self.want:
                return
            self._active = True
            self._current = {"marker": marker, "rows": []}
            return
        if not self._active:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._capturing = True
            self._cell_tag = tag
            self._cell_name = ad.get("data-col", "")
            self._cell_text = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._active and self._depth == 1 and self._current is not None:
                self.tables.append(self._current)
                self._active = False
                self._current = None
            if self._depth:
                self._depth -= 1
            return
        if not self._active:
            return
        if tag in {"th", "td"} and self._capturing and self._row is not None:
            text = re.sub(r"\s+", " ", self._cell_text).strip()
            self._row.append(_Cell(tag=self._cell_tag, name=self._cell_name, text=text))
            self._capturing = False
        elif tag == "tr" and self._row is not None and self._current is not None:
            if self._row:
                self._current["rows"].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._cell_text += data


def extract_table(html: str, marker: str | None = "invoices") -> ExtractedTable:
    parser = _TableParser(want=marker)
    parser.feed(html)
    parser.close()
    if not parser.tables:
        raise ValueError("no matching table found on the page")
    raw = parser.tables[0]
    rows: list[list[_Cell]] = raw["rows"]
    if not rows:
        raise ValueError("table is empty")

    header_row = rows[0]
    body_rows = rows[1:]
    if any(c.tag != "th" for c in header_row) and body_rows and all(c.tag == "th" for c in rows[0]):
        header_row = rows[0]
        body_rows = rows[1:]
    elif all(c.tag == "th" for c in header_row):
        pass
    else:
        # first row is headers anyway (login-walled demo always uses <th>)
        pass

    used: dict[str, int] = {}
    columns: list[Column] = []
    for cell in header_row:
        name = cell.name or slug(cell.text)
        if name in used:
            used[name] += 1
            name = f"{name}_{used[name]}"
        else:
            used[name] = 1
        columns.append(Column(name=name, header=cell.text))

    raw_rows: list[list[str]] = []
    string_rows: list[dict[str, str]] = []
    for cells in body_rows:
        values = [c.text for c in cells]
        raw_rows.append(values)
        string_rows.append(
            {columns[i].name: (values[i] if i < len(values) else "") for i in range(len(columns))}
        )

    if string_rows:
        for col in columns:
            samples = [r[col.name] for r in string_rows if r.get(col.name, "").strip()]
            col.sample = samples[0] if samples else ""
            types = {infer_type(s) for s in samples} if samples else {"string"}
            if types == {"number"}:
                col.type = "number"
            elif types == {"date"}:
                col.type = "date"
            else:
                col.type = "date" if types <= {"date", "string"} and "date" in types and len(types) == 1 else (
                    "number" if types == {"number"} else "string"
                )
            col.required = all(r.get(col.name, "").strip() for r in string_rows)

    coerced: list[dict[str, Any]] = []
    for r in string_rows:
        item: dict[str, Any] = {}
        for c in columns:
            raw_val = r.get(c.name, "")
            item[c.name] = coerce(raw_val, c.type) if str(raw_val).strip() else None
        coerced.append(item)

    return ExtractedTable(
        selector=f"[data-portalc='{raw['marker']}']" if raw.get("marker") else "table",
        marker=raw.get("marker") or "",
        columns=columns,
        rows=coerced,
        raw_rows=raw_rows,
    )
