"""Grammy Hall of Fame recipients (singles and tracks only), from four Wikipedia pages.

The Hall of Fame is unranked, so every row gets the pseudo rank 250 in the fusion
(``source_list.pseudo_rank``); ``list_entry.rank`` is just the row order. Recordings must
be 25 years old to be inducted, which makes this list strong on the pre-1970 roots.
"""

from __future__ import annotations

import re

from . import wikitext as wt
from .model import Entry, ListSource, Loaded
from .net import Net
from .wikipedia import fetch_page

LIST_ID = "grammy_hof"
PAGES = [f"List of Grammy Hall of Fame Award recipients ({p})" for p in ("A–D", "E–I", "J–P", "Q–Z")]
WEIGHT = 1.5
PSEUDO_RANK = 250
FORMATS = ("single", "track", "song")


def parse_page(wikitext: str, start_rank: int = 1) -> list[Entry]:
    out: list[Entry] = []
    rank = start_rank
    for table in wt.parse_tables(wikitext):
        header = table.header_row()
        c_title = wt.column_index(header, "title")
        c_artist = wt.column_index(header, "artist")
        c_year = wt.column_index(header, "year of release", "year")
        c_fmt = wt.column_index(header, "format")
        if None in (c_title, c_artist, c_fmt):
            continue
        for row in table.data_rows():
            if len(row) <= max(c_title, c_artist, c_fmt):
                continue
            fmt = row[c_fmt].plain.lower()
            if not fmt.startswith(FORMATS):
                continue
            title_cell = row[c_title].text
            title = wt.unquote(wt.plain(title_cell))
            artist = row[c_artist].plain
            if not title or not artist:
                continue
            ym = re.search(r"\b(1[89]\d\d|20\d\d)\b", row[c_year].plain) if c_year is not None else None
            links = wt.links(title_cell)
            out.append(Entry(LIST_ID, rank, title, artist, list_year=int(ym.group(1)) if ym else None,
                             wiki_link=links[0] if links else None,
                             artist_links=tuple(wt.links(row[c_artist].text))))
            rank += 1
    return out


def load(net: Net, *, refresh: bool = False) -> Loaded:
    entries: list[Entry] = []
    revisions, shas, times = [], [], []
    for title in PAGES:
        got = fetch_page(net, title, refresh=refresh)
        if got is None:
            continue
        text, meta = got
        entries += parse_page(text, start_rank=len(entries) + 1)
        revisions.append(meta.get("revision") or "")
        shas.append((meta.get("sha256") or "")[:16])
        times.append(meta.get("retrieved_at") or "")
    src = ListSource(
        list_id=LIST_ID, name="Grammy Hall of Fame (singles and tracks, via Wikipedia)", weight=WEIGHT,
        url="https://en.wikipedia.org/wiki/Grammy_Hall_of_Fame", revision=",".join(revisions),
        sha256=",".join(shas), retrieved_at=max(times) if times else None,
        license_note="Wikipedia CC BY-SA 4.0 (4 pages A-D, E-I, J-P, Q-Z)", pseudo_rank=PSEUDO_RANK,
    )
    return Loaded(src, entries)
