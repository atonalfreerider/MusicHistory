"""Create a scratch pipeline DB with the fixture works (stand-in for the canon stage).

Usage (PowerShell):
    $env:MUSICHISTORY_DATA = "<scratch>\\fetch-test"
    $env:MUSICHISTORY_CACHE = "C:\\Users\\johnb\\Desktop\\MusicHistory\\data\\cache"
    .venv\\Scripts\\python tests\\sources\\make_fixture_db.py
    .venv\\Scripts\\python -m musichistory fetch --midicollection-below 0

The fixture lists 43 well-known works from 1952 to 2024 (titles, artists and years only)
including the tricky ones for matching: parenthesised titles, "With or Without You",
featuring credits, a cover whose original artist is also searched (Respect, Hound Dog),
a short title whose famous namesake is by someone else (Hero), and post-2015 hits that
Lakh lacks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FIXTURE = Path(__file__).with_name("fixture_works.json")


def load_fixture(conn) -> int:
    works = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for w in works:
        conn.execute(
            "INSERT OR REPLACE INTO work(work_id, title, canonical_artist, original_artist, search_artists,"
            " work_year, effective_year, year_confidence, canon_rank, rrf_score, in_pool)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
            (w["work_id"], w["title"], w["artist"], w.get("original_artist"),
             json.dumps(w["search_artists"], ensure_ascii=False), w["work_year"], w["work_year"], "high",
             w["canon_rank"], 1.0 / w["canon_rank"]))
    conn.commit()
    return len(works)


if __name__ == "__main__":
    from musichistory import config, db

    conn = db.connect()
    n = load_fixture(conn)
    print(f"{n} fixture works in {config.PIPELINE_DB}")
