"""Rolling Stone "500 Greatest Songs of All Time", 2021 and 2004 editions.

rollingstone.com is not fetched (robots.txt blocks AI agents). Both editions come from
GitHub mirrors pinned by commit. The mirrors carry no licence, so only rank, title,
artist and year are stored, with a citation. Known errors in the 2021 file ("Brass in
Pocket" 1879, "What'd I Say" 1957) are left as they are: the year rules catch them.
"""

from __future__ import annotations

import csv
import io

from .model import Entry, ListSource, Loaded
from .net import Net

RS2021_ID = "rs500_2021"
RS2021_COMMIT = "de2e509db48e98a8551d5032ce27d19b276d4c57"
RS2021_URL = (f"https://raw.githubusercontent.com/ossings/rolling_stone_top_500_songs_2021/"
              f"{RS2021_COMMIT}/top_500_songs.csv")
RS2004_ID = "rs500_2004"
RS2004_COMMIT = "e90def7bee56ce36dc5193fc7f82954e2fe4a0ee"
RS2004_URL = (f"https://raw.githubusercontent.com/Computational-Cognitive-Musicology-Lab/"
              f"CoCoPops-RollingStone-legacy/{RS2004_COMMIT}/RollingStone_The500GreatestSongsOfAllTime_2004.tsv")
WEIGHTS = {RS2021_ID: 4.0, RS2004_ID: 3.0}
NOTE = "Rolling Stone 500 Greatest Songs of All Time; GitHub mirror without licence: rank/title/artist/year only"


def parse_2021(text: str) -> list[Entry]:
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            rank = int(r["Rank"])
        except (KeyError, ValueError):
            continue
        y = (r.get("Year") or "").strip()
        out.append(Entry(RS2021_ID, rank, r["Title"].strip(), r["Artist"].strip(),
                         list_year=int(y) if y.isdigit() else None))
    return sorted(out, key=lambda e: e.rank)


def parse_2004(text: str) -> list[Entry]:
    out = []
    for r in csv.DictReader(io.StringIO(text), delimiter="\t"):
        try:
            rank = int(r["RANK"])
        except (KeyError, ValueError):
            continue
        out.append(Entry(RS2004_ID, rank, r["TITLE"].strip(), r["ARTIST"].strip()))
    return sorted(out, key=lambda e: e.rank)


def load(net: Net, *, refresh: bool = False) -> list[Loaded]:
    out = []
    for list_id, url, commit, fname, parser, edition in (
        (RS2021_ID, RS2021_URL, RS2021_COMMIT, "rs500_2021.csv", parse_2021, "2021"),
        (RS2004_ID, RS2004_URL, RS2004_COMMIT, "rs500_2004.tsv", parse_2004, "2004"),
    ):
        data, meta = net.fetch_file(fname, url, revision=commit, refresh=refresh)
        src = ListSource(list_id=list_id, name=f"Rolling Stone 500 Greatest Songs ({edition})",
                         weight=WEIGHTS[list_id], edition=edition, url=url, revision=commit,
                         sha256=meta.get("sha256"), retrieved_at=meta.get("retrieved_at"), license_note=NOTE)
        out.append(Loaded(src, parser(data.decode("utf-8-sig", errors="replace"))))
    return out
