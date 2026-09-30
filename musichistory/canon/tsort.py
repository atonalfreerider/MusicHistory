"""tsort.info "top 5000 songs" v2.9.0001: a fusion of 143 charts worldwide, 1901-2017.

One CSV download (the site asks for its CSVs to be used instead of crawling; robots.txt
sets a 12 s crawl delay, which ``net.INTERVALS`` enforces). Reuse requires crediting
tsort.info with a link and the version. Its ``year`` is the charting recording's year,
not the composition's (Jeff Buckley's "Hallelujah" is listed as 2007).
"""

from __future__ import annotations

import csv
import io

from .model import Entry, ListSource, Loaded
from .net import Net

LIST_ID = "tsort_5000"
URL = "https://tsort.info/csv/top5000songs-2-9-0001.csv"
FILE = "tsort_top5000songs-2-9-0001.csv"
WEIGHT = 6.0


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse(text: str) -> list[Entry]:
    out: list[Entry] = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            rank = int(r["position"])
        except (KeyError, ValueError):
            continue
        year = r.get("year", "").strip()
        out.append(Entry(LIST_ID, rank, r["name"].strip(), r["artist"].strip(),
                         list_year=int(year) if year.isdigit() else None))
    return out


def load(net: Net, *, refresh: bool = False) -> Loaded:
    data, meta = net.fetch_file(FILE, URL, revision="v2.9.0001", refresh=refresh)
    src = ListSource(
        list_id=LIST_ID, name="tsort.info top 5000 songs", weight=WEIGHT, edition="v2.9.0001",
        url=URL, revision=meta.get("revision") or "v2.9.0001", sha256=meta.get("sha256"),
        retrieved_at=meta.get("retrieved_at"),
        license_note="Credit: tsort.info (https://tsort.info), top 5000 songs v2.9.0001",
    )
    return Loaded(src, parse(decode(data)))
