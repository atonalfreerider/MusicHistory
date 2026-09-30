"""Spotify most-streamed songs (top 100), from Wikipedia's "List of Spotify streaming records".

A small-weight counterweight to tsort, whose data stops in 2017. The song cell is a row
header (``! scope="row" |``) and the release date a ``{{Date table sorting|...}}``.
"""

from __future__ import annotations

import re

from . import wikitext as wt
from .model import Entry, ListSource, Loaded
from .net import Net
from .wikipedia import fetch_page

LIST_ID = "spotify_top"
PAGE = "List of Spotify streaming records"
WEIGHT = 2.0


def parse_page(wikitext: str) -> list[Entry]:
    for table in wt.parse_tables(wikitext):
        header = table.header_row()
        c_rank = wt.column_index(header, "rank")
        c_song = wt.column_index(header, "song")
        c_artist = wt.column_index(header, "artist")
        c_date = wt.column_index(header, "release", "date")
        if None in (c_rank, c_song, c_artist):
            continue
        out: list[Entry] = []
        for row in table.data_rows():
            if len(row) <= max(c_rank, c_song, c_artist):
                continue
            m = re.match(r"\s*(\d+)", row[c_rank].plain)
            if not m:
                continue
            title = wt.unquote(row[c_song].plain)
            artist = row[c_artist].plain
            ym = re.search(r"\b(19\d\d|20\d\d)\b", row[c_date].plain) if c_date is not None and len(row) > c_date else None
            links = wt.links(row[c_song].text)
            if title and artist:
                out.append(Entry(LIST_ID, int(m.group(1)), title, artist,
                                 list_year=int(ym.group(1)) if ym else None,
                                 wiki_link=links[0] if links else None,
                                 artist_links=tuple(wt.links(row[c_artist].text))))
        if out:  # the first ranked song table on the page is the most-streamed songs
            return out
    return []


def load(net: Net, *, refresh: bool = False) -> Loaded:
    got = fetch_page(net, PAGE, refresh=refresh)
    text, meta = got if got else ("", {})
    src = ListSource(list_id=LIST_ID, name="Spotify most-streamed songs (via Wikipedia)", weight=WEIGHT,
                     url=meta.get("url"), revision=meta.get("revision"), sha256=meta.get("sha256"),
                     retrieved_at=meta.get("retrieved_at"), license_note="Wikipedia CC BY-SA 4.0")
    return Loaded(src, parse_page(text))
