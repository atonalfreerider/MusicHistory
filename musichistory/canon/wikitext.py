"""A small, forgiving wikitable parser for the Wikipedia list pages the canon stage reads.

Wikipedia list tables were written by many hands over two decades, so the same kind of
page mixes conventions: one row per line (``|1 || "[[A]]" || [[B]]``) or one cell per line,
a rank alone on its own line (``| scope="row" | 1``), header cells inside data rows
(``! scope="row" | "[[Song]]"``), ``rowspan`` on artist cells, captions (``|+``), and
references or comments anywhere. This module turns a table into a rectangular grid of
cells (row/col spans expanded) that keeps each cell's raw wikitext, so callers can take
both the display text and the link targets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LINK = re.compile(r"\[\[([^\]\|]+)(?:\|((?:[^\]]|\](?!\]))*))?\]\]")
_ATTRS = re.compile(r"""^\s*(?:[A-Za-z][A-Za-z0-9_-]*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'|]+)\s*)+$""")
_SPAN = re.compile(r"""\b(rowspan|colspan)\s*=\s*["']?\s*(\d+)""", re.I)
_NON_ARTICLE = re.compile(r"^(?:file|image|category|media|wikt|wiktionary):", re.I)


def strip_noise(text: str) -> str:
    """Drop references, comments and ``<br>`` noise that never carries table content."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>/]*/>", "", text, flags=re.I)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S | re.I)
    text = re.sub(r"\{\{\s*(?:efn|refn|sfn|r|cn|citation needed)\b[^{}]*\}\}", "", text, flags=re.I)
    return text


def _template(m: re.Match) -> str:
    name, _, rest = m.group(1).partition("|")
    name = name.strip().lower()
    args = rest.split("|") if rest else []
    pos = [a.strip() for a in args if not re.match(r"^\s*[\w -]+\s*=", a)]
    if name in ("sort", "sortname"):
        if name == "sortname" and len(pos) >= 2:
            return f"{pos[0]} {pos[1]}"
        return pos[-1] if pos else ""
    if name in ("nowrap", "nobr", "small", "big", "abbr", "lang", "date table sorting", "dts",
                "center", "nobold", "noitalic", "sronly", "nbsp"):
        if name == "lang" and len(pos) >= 2:
            return pos[1]
        if name in ("date table sorting", "dts"):
            return " ".join(pos)
        if name == "nbsp":
            return " "
        return pos[0] if pos else ""
    if name in ("'s",):
        return "'s"
    return ""


def plain(s: str) -> str:
    """Display text of a wikitext fragment: links to their labels, templates resolved or dropped."""
    s = strip_noise(s)
    s = _LINK.sub(lambda m: "" if _NON_ARTICLE.match(m.group(1)) else (m.group(2) or m.group(1)), s)
    prev = None
    while prev != s:  # innermost templates first
        prev = s
        s = re.sub(r"\{\{([^{}]*)\}\}", _template, s)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("'''", "").replace("''", "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", s).strip()


def links(s: str) -> list[str]:
    """Article link targets in a fragment, ``#anchor`` removed, File/Category links skipped."""
    out = []
    for m in _LINK.finditer(strip_noise(s)):
        target = m.group(1).strip()
        if _NON_ARTICLE.match(target) or target.startswith("#"):
            continue
        target = target.split("#")[0].strip().lstrip(":")
        if target:
            out.append(target[0].upper() + target[1:])
    return out


def _depth_split(text: str, sep: str) -> list[str]:
    """Split on ``sep`` outside ``[[...]]`` and ``{{...}}``."""
    parts: list[str] = []
    depth = 0
    i = start = 0
    n = len(text)
    while i < n:
        two = text[i : i + 2]
        if two in ("[[", "{{"):
            depth += 1
            i += 2
            continue
        if two in ("]]", "}}"):
            depth = max(0, depth - 1)
            i += 2
            continue
        if depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return parts


def _split_attrs(cell: str) -> tuple[str, str]:
    """``'rowspan="2" | [[X]]'`` -> ``('rowspan="2"', '[[X]]')`` when the prefix is attributes."""
    parts = _depth_split(cell, "|")
    if len(parts) >= 2 and _ATTRS.match(parts[0]):
        return parts[0].strip(), "|".join(parts[1:]).strip()
    return "", cell.strip()


@dataclass
class Cell:
    text: str                 # raw wikitext of the cell content
    header: bool = False      # written with '!'
    attrs: str = ""
    rowspan: int = 1
    colspan: int = 1

    @property
    def plain(self) -> str:
        return plain(self.text)

    @property
    def links(self) -> list[str]:
        return links(self.text)

    @property
    def is_row_header(self) -> bool:
        return self.header and "scope" in self.attrs.lower() and "row" in self.attrs.lower()


@dataclass
class Table:
    caption: str = ""
    rows: list[list[Cell]] = field(default_factory=list)   # expanded grid, header rows included

    def header_row(self) -> list[str] | None:
        for row in self.rows:
            if row and all(c.header and not c.is_row_header for c in row):
                return [c.plain.lower() for c in row]
        return None

    def data_rows(self) -> list[list[Cell]]:
        return [r for r in self.rows if r and not all(c.header and not c.is_row_header for c in r)]


def find_tables(wikitext: str) -> list[str]:
    """Top-level ``{| ... |}`` blocks (nested tables stay inside their parent)."""
    out = []
    depth = 0
    start = -1
    for m in re.finditer(r"(?m)^\s*(\{\||\|\})", wikitext):
        tok = m.group(1)
        if tok == "{|":
            if depth == 0:
                start = m.start()
            depth += 1
        elif depth > 0:
            depth -= 1
            if depth == 0:
                out.append(wikitext[start : m.end()])
    return out


def _raw_rows(table: str) -> tuple[str, list[list[Cell]]]:
    lines = strip_noise(table).split("\n")
    caption = ""
    rows: list[list[Cell]] = []
    cur: list[Cell] | None = None
    for raw in lines[1:]:  # first line is '{| attrs'
        line = raw.strip()
        if not line:
            continue
        if line.startswith("|}"):
            break
        if line.startswith("|+"):
            caption = plain(_split_attrs(line[2:])[1])
            continue
        if line.startswith("|-"):
            cur = []
            rows.append(cur)
            continue
        if line.startswith("!") or line.startswith("|"):
            if cur is None:
                cur = []
                rows.append(cur)
            header = line.startswith("!")
            body = line[1:]
            pieces = _depth_split(body, "!!") if header else [body]
            cells: list[str] = []
            for p in pieces:
                cells.extend(_depth_split(p, "||"))
            for c in cells:
                attrs, text = _split_attrs(c)
                spans = {k.lower(): int(v) for k, v in _SPAN.findall(attrs)}
                cur.append(Cell(text, header, attrs, max(1, spans.get("rowspan", 1)), max(1, spans.get("colspan", 1))))
        elif cur:
            cur[-1].text += "\n" + raw  # continuation of a multi-line cell
    return caption, [r for r in rows if r]


def parse_table(table: str) -> Table:
    caption, raw_rows = _raw_rows(table)
    grid: list[list[Cell]] = []
    pending: dict[int, tuple[int, Cell]] = {}  # column -> (rows still to fill, cell)
    for raw in raw_rows:
        row: list[Cell] = []
        queue = list(raw)
        col = 0
        while queue or col in pending:
            if col in pending:
                left, cell = pending[col]
                row.append(cell)
                if left > 1:
                    pending[col] = (left - 1, cell)
                else:
                    del pending[col]
                col += 1
                continue
            cell = queue.pop(0)
            for _ in range(cell.colspan):
                row.append(cell)
                if cell.rowspan > 1:
                    pending[col] = (cell.rowspan - 1, cell)
                col += 1
        grid.append(row)
    return Table(caption, grid)


def parse_tables(wikitext: str) -> list[Table]:
    return [parse_table(t) for t in find_tables(wikitext)]


def column_index(header: list[str] | None, *names: str, default: int | None = None) -> int | None:
    """Index of the first header cell that starts with one of ``names``."""
    if header:
        for name in names:
            for i, h in enumerate(header):
                if h.startswith(name):
                    return i
    return default


def split_double_a_side(title_cell: str) -> list[str]:
    """``"[[A]]" / "[[B]]"`` -> two cell fragments; anything else -> one."""
    parts = [p for p in re.split(r'"\s*/\s*"', title_cell) if p.strip()]
    return parts if len(parts) >= 2 else [title_cell]


def unquote(title: str) -> str:
    """Title inside its surrounding quotes.

    '"The Cover of "Rolling Stone""' keeps the inner pair; '"Ain't Misbehavin'" (piano solo)'
    drops the trailing description.
    """
    t = title.strip().replace("“", '"').replace("”", '"')
    m = re.match(r'^"(.*)"(\s*\([^()]*\))?\s*$', t)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return t.strip('"').strip()
