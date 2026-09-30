"""Optional Acclaimed Music import from a CSV the user exported by hand.

acclaimedmusic.net sits behind a bot challenge, so it is never fetched. ``--acclaimed
<csv>`` accepts any CSV with a rank column (rank/position/pos/no) and title and artist
columns (title/song/name, artist/performer); a year column is used when present.
"""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path

from .model import Entry, ListSource, Loaded
from .net import now_iso

LIST_ID = "acclaimed"
WEIGHT = 5.0


def _col(fields: list[str], *names: str) -> str | None:
    low = {f.lower().strip(): f for f in fields}
    for n in names:
        if n in low:
            return low[n]
    return None


def load(path: Path) -> Loaded:
    data = path.read_bytes()
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    fields = reader.fieldnames or []
    c_rank = _col(fields, "rank", "position", "pos", "no", "no.", "#")
    c_title = _col(fields, "title", "song", "name", "track")
    c_artist = _col(fields, "artist", "performer", "artists")
    c_year = _col(fields, "year", "released", "release year")
    if not (c_rank and c_title and c_artist):
        raise ValueError(f"{path}: need rank, title and artist columns, got {fields}")
    entries = []
    for r in reader:
        m = re.match(r"\s*(\d+)", r.get(c_rank) or "")
        if not m or not (r.get(c_title) or "").strip():
            continue
        ym = re.search(r"\b(1[89]\d\d|20\d\d)\b", r.get(c_year) or "") if c_year else None
        entries.append(Entry(LIST_ID, int(m.group(1)), r[c_title].strip(), (r.get(c_artist) or "").strip(),
                             list_year=int(ym.group(1)) if ym else None))
    src = ListSource(list_id=LIST_ID, name="Acclaimed Music (user export)", weight=WEIGHT,
                     url=str(path), sha256=hashlib.sha256(data).hexdigest(), retrieved_at=now_iso(),
                     license_note="user-supplied export of acclaimedmusic.net; not redistributed")
    return Loaded(src, entries)
