"""Billboard year-end singles lists 1946..last complete year, from Wikipedia.

billboard.com itself is not fetched (its robots.txt blocks AI agents); the Wikipedia
pages reproduce the year-end tables with a link to each song's article, which is what
makes them the best-resolved source. Page titles and table layouts differ by era:

* 1946-48 "Billboard year-end top singles of Y": first column "No. (Rank)" such as
  ``28 (32)``; the number in brackets is the competition rank used here.
* 1949-55 "... top 30 singles of Y", 1956-58 "... top 50 singles of Y": ``rowspan`` on
  artist cells (two consecutive hits by one act).
* 1959+ "Billboard Year-End Hot 100 singles of Y": captions from 2010, and from 2025 the
  rank on its own line (``| scope="row" | 1``).
* Every era: tied ranks, and double A-sides written ``"[[A]]" / "[[B]]"``, which are split
  into two rows (the B side weighted 0.5).

A year-end list's year is the chart year (the 1975 chart ran from November 1974), so it
is an upper bound on release, never a release date.
"""

from __future__ import annotations

import re

from . import wikitext as wt
from .model import Entry, ListSource, Loaded
from .net import Net
from .wikipedia import fetch_page

FIRST_YEAR = 1946
WEIGHT = 1.0


def page_title(year: int) -> str:
    if year <= 1948:
        return f"Billboard year-end top singles of {year}"
    if year <= 1955:
        return f"Billboard year-end top 30 singles of {year}"
    if year <= 1958:
        return f"Billboard year-end top 50 singles of {year}"
    return f"Billboard Year-End Hot 100 singles of {year}"


def list_id(year: int) -> str:
    return f"billboard_ye_{year}"


def _rank(cell_text: str) -> int | None:
    txt = wt.plain(cell_text)
    m = re.search(r"\(\s*(\d+)\s*\)", txt)  # 1946-48: "No. (Rank)"
    if m:
        return int(m.group(1))
    m = re.match(r"\s*(?:T-?|=)?\s*(\d+)", txt)
    return int(m.group(1)) if m else None


def _main_table(wikitext: str) -> wt.Table | None:
    best = None
    for table in wt.parse_tables(wikitext):
        header = table.header_row()
        if header and wt.column_index(header, "title") is not None and table.data_rows():
            if best is None or len(table.data_rows()) > len(best.data_rows()):
                best = table
    return best


def parse_page(wikitext: str, year: int) -> list[Entry]:
    table = _main_table(wikitext)
    if table is None:
        return []
    header = table.header_row()
    c_rank = wt.column_index(header, "no", "rank", "#", "pos", default=0)
    c_title = wt.column_index(header, "title", "song", default=1)
    c_artist = wt.column_index(header, "artist", "performer", default=2)
    out: list[Entry] = []
    seen: set[tuple] = set()
    for row in table.data_rows():
        if len(row) <= max(c_rank, c_title, c_artist):
            continue
        rank = _rank(row[c_rank].text)
        if rank is None:
            continue
        artist_cell = row[c_artist].text
        artist = wt.plain(artist_cell)
        artist_links = tuple(wt.links(artist_cell))
        sides = wt.split_double_a_side(row[c_title].text)
        for i, side in enumerate(sides):
            title = wt.unquote(wt.plain(side))
            if not title or not artist:
                continue
            links = wt.links(side)
            key = (rank, title, artist)
            if key in seen:
                continue
            seen.add(key)
            out.append(Entry(list_id(year), rank, title, artist, list_year=year,
                             wiki_link=links[0] if links else None,
                             weight_factor=1.0 if i == 0 else 0.5, artist_links=artist_links))
    return out


def load(net: Net, last_year: int, *, refresh: bool = False, log=print) -> list[Loaded]:
    out: list[Loaded] = []
    for year in range(FIRST_YEAR, last_year + 1):
        title = page_title(year)
        got = fetch_page(net, title, refresh=refresh)
        if got is None:
            log(f"  billboard {year}: page '{title}' not found, skipped")
            continue
        text, meta = got
        entries = parse_page(text, year)
        src = ListSource(
            list_id=list_id(year), name=f"Billboard year-end singles {year} (via Wikipedia)", weight=WEIGHT,
            edition=str(year), url=meta.get("url"), revision=meta.get("revision"), sha256=meta.get("sha256"),
            retrieved_at=meta.get("retrieved_at"), license_note="Wikipedia CC BY-SA 4.0; chart data Billboard",
        )
        out.append(Loaded(src, entries))
    return out
