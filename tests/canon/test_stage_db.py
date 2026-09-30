"""The stage's database writes are idempotent and respect other stages' columns."""

from __future__ import annotations

import csv

from musichistory import db
from musichistory.canon import stage
from musichistory.canon.fusion import fuse
from musichistory.canon.model import Entry, ListSource
from musichistory.canon.years import Evidence

SOURCES = {"tsort_5000": ListSource("tsort_5000", "tsort", 6.0), "rs500_2021": ListSource("rs500_2021", "rs", 4.0)}


def build(titles):
    entries = [Entry("tsort_5000", i + 1, t, "Artist", list_year=1970 + i, work_id=f"R{i:012d}",
                     resolution_method="normalized_key", resolution_confidence=0.5) for i, t in enumerate(titles)]
    works = fuse(entries, SOURCES)
    rows = {w.work_id: dict(work_id=w.work_id, title=w.title, canonical_artist=w.artist, original_artist=None,
                            search_artists='["Artist"]', work_date=str(1970), work_date_precision=9, work_year=1970,
                            effective_year=1970, year_confidence="medium", traditional=0, wikidata_qid=None,
                            mb_work_id=None, first_chart_week=None, rrf_score=w.score, canon_rank=w.canon_rank,
                            in_pool=1) for w in works}
    ev = {w.work_id: [Evidence("list:tsort_5000", "1970")] for w in works}
    return entries, works, rows, ev


def test_rerun_replaces_rows_and_keeps_selected(tmp_path):
    conn = db.connect(tmp_path / "p.sqlite")
    entries, works, rows, ev = build(["A", "B", "C"])
    infl = [("R000000000000", "R000000000001", "control_positive", "{}")]
    stage.write_db(conn, list(SOURCES.values()), entries, works, rows, ev, infl)
    stage.write_db(conn, list(SOURCES.values()), entries, works, rows, ev, infl)
    count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert (count("source_list"), count("list_entry"), count("work"), count("year_evidence"), count("known_influence")) == (2, 3, 3, 3, 1)

    conn.execute("UPDATE work SET selected = 1 WHERE work_id = 'R000000000001'")
    conn.execute("INSERT INTO candidate(work_id, source, md5) VALUES ('R000000000002', 'lakh', 'x')")
    conn.commit()
    entries, works, rows, ev = build(["A", "B"])          # C disappeared from the lists
    stage.write_db(conn, list(SOURCES.values()), entries, works, rows, ev, [])
    got = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT work_id, selected, canon_rank, in_pool FROM work")}
    assert got["R000000000001"] == (1, 2, 1)               # select's column survives a rerun
    assert got["R000000000002"] == (0, None, 0)            # kept (a candidate points at it), unranked
    assert count("known_influence") == 0 and count("list_entry") == 2


def test_csv_lists_pooled_works_in_rank_order(tmp_path):
    _, works, rows, _ = build(["A", "B", "C"])
    rows[works[1].work_id]["in_pool"] = 0
    n = stage.write_csv(tmp_path / "top.csv", works, rows)
    with open(tmp_path / "top.csv", encoding="utf-8") as f:
        got = list(csv.DictReader(f))
    assert n == 2 and list(got[0]) == stage.CSV_COLUMNS
    assert [r["canon_rank"] for r in got] == ["1", "3"]
    assert got[0]["lists"] == "tsort_5000#1"
