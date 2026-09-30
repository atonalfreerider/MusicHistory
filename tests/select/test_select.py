"""select stage with a fake analysis engine (the analyze stage's API is faked here)."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import mido
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "midi"))

import midigen  # noqa: E402
from musichistory import config, db  # noqa: E402
from musichistory import select as sel  # noqa: E402
from musichistory.midi import process  # noqa: E402
from musichistory.sources import base  # noqa: E402
from musichistory.sources.base import Hit, Work  # noqa: E402


# ------------------------------------------------------------------ static quality
def feats(**over) -> dict:
    return process(midigen.song(**over)).features


def cand(**over) -> dict:
    d = {"orig_name": "Artist/Title.mid", "match_class": "accept", "lmd_match_score": None, "title": "Title"}
    d.update(over)
    return d


def test_static_quality_orders_arrangements():
    full, comp = sel.static_quality(feats(), cand(lmd_match_score=0.8))
    assert comp["tracks"] == 0.75 and comp["drums"] == 1.0 and comp["melody"] == 1.0 and comp["lmd"] == 1.0
    assert comp["penalties"] == []
    no_lmd, _ = sel.static_quality(feats(), cand())
    probable, _ = sel.static_quality(feats(), cand(match_class="probable", lmd_match_score=0.8))
    piano = feats(tracks=[{"name": "Piano", "channel": 0, "program": 0, "pitches": [60, 64, 67], "step": 1.0, "chord": True},
                          {"name": "Piano LH", "channel": 1, "program": 1, "pitches": [36, 43], "step": 2.0}])
    piano_q, pc = sel.static_quality(piano, cand())
    short_q, sc = sel.static_quality(feats(bars=25), cand())      # 50 s
    ring_q, rc = sel.static_quality(feats(), cand(orig_name="POLYPHONE RINGTONES 2/Title.mid"))
    medley_q, mc = sel.static_quality(feats(), cand(orig_name="Beatles Medley.mid"))
    assert full > probable > no_lmd > piano_q
    assert "piano_only" in pc["penalties"] and "short" in sc["penalties"]
    assert "ringtone" in rc["penalties"] and "medley" in mc["penalties"]
    assert no_lmd > ring_q and no_lmd > medley_q and no_lmd > short_q
    assert 0.0 <= min(full, piano_q, short_q, ring_q, medley_q) and full <= 1.0
    # a medley work title does not penalize its own medley file
    assert "medley" not in sel.static_quality(feats(), cand(orig_name="x Medley.mid", title="Abbey Road Medley"))[1]["penalties"]


def test_karaoke_thin_penalty():
    thin = feats(tracks=[{"name": "Melody", "channel": 0, "program": 0, "pitches": [60, 62], "step": 1.0}])
    thin["karaoke"] = True
    q, comp = sel.static_quality(thin, cand())
    assert "karaoke_thin" in comp["penalties"]


def test_lead_track_choice():
    f = feats()
    assert sel.lead_track(f) == 1  # "Lead Vocal" by name
    f2 = process(midigen.song(fmt=0)).features
    assert sel.lead_track(f2) is None  # nothing named: leave it to the analyze stage
    f2["lyric_melody_track"] = 3
    assert sel.lead_track(f2) == 3


def test_combine_renormalizes():
    assert sel.combine(0.8, 0.6, 0.4) == pytest.approx(0.35 * 0.8 + 0.40 * 0.6 + 0.25 * 0.4, abs=1e-4)
    assert sel.combine(0.8, None, None) == 0.8
    assert sel.combine(0.8, 0.6, None) == pytest.approx((0.35 * 0.8 + 0.40 * 0.6) / 0.75, abs=1e-4)
    assert sel.combine(None, None, None) is None


# ------------------------------------------------------------------ final set
def test_final_set_floor_and_target():
    # 30 top works all from 1990; years 1950-1952 have lower-ranked works
    works = [(f"A{i}", i, 1990) for i in range(1, 31)]
    works += [(f"B{y}{k}", 100 + (y - 1950) * 10 + k, y) for y in (1950, 1951, 1952) for k in range(7)]
    works.append(("N1", None, None))
    ids = sel.final_set(works, target=25, floor=5, first=1950, last=1952)
    assert len(ids) == 25
    years = [next(w[2] for w in works if w[0] == i) for i in ids]
    assert all(years.count(y) == 5 for y in (1950, 1951, 1952))
    assert years.count(1990) == 10
    assert set(i for i in ids if i.startswith("A")) == {f"A{i}" for i in range(1, 11)}  # best-ranked kept
    assert all(i[:5] in {"B1950", "B1951", "B1952"} and int(i[5:]) < 5 for i in ids if i.startswith("B"))


def test_final_set_short_year_keeps_what_exists():
    works = [(f"A{i}", i, 2000) for i in range(1, 11)] + [("B1", 50, 1960), ("B2", 51, 1960)]
    ids = sel.final_set(works, target=8, floor=5, first=1960, last=1960)
    assert "B1" in ids and "B2" in ids and len(ids) == 8


def test_default_floor_last():
    import datetime as dt

    assert sel.default_floor_last(dt.date(2026, 9, 29)) == 2024


# ------------------------------------------------------------------ select_work with a fake engine
class FakeEngine:
    """Identity = the label of the transcription family encoded in the file's bar count."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.analyzed: list[str] = []

    def analyze(self, midi_path, workdir, lead_track):
        self.analyzed.append(Path(midi_path).name)
        if any(f in Path(midi_path).name for f in self.fail):
            raise RuntimeError("PatternPrep crashed")
        mid = mido.MidiFile(str(midi_path))
        return {"end_beat": round(mid.length, 1), "lead": lead_track}

    def extract(self, slim, features):
        return "good" if slim["end_beat"] < 100 else "bad"

    def chord_agreement(self, a, b):
        return 1.0 if a == b else 0.1

    def melody_agreement(self, a, b):
        return 0.9 if a == b else 0.0

    def hooktheory_agreement(self, ident, sections):
        return 0.8 if ident == "good" else 0.2


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "CANDIDATES", tmp_path / "data" / "candidates")
    monkeypatch.setattr(config, "ANALYSIS_WORK", tmp_path / "data" / "analysis-work")
    monkeypatch.setattr(config, "PIPELINE_DB", tmp_path / "data" / "p.sqlite")
    conn = db.connect(config.PIPELINE_DB)
    conn.executescript(sel.SCHEMA)
    base.ensure_schema(conn)
    return conn


def _add(conn, work: Work, source: str, ref: str, data: bytes, **kw) -> None:
    h = Hit(source, ref, kw.pop("name", f"{work.artist}/{work.title}.mid"), 100, 100, kw.pop("cls", "accept"), **kw)
    assert base.ingest(conn, work, h, data) in ("new", "invalid")
    conn.commit()


def _with_text(data: bytes, text: str) -> bytes:
    mid = mido.MidiFile(file=io.BytesIO(data))
    mid.tracks[0].insert(0, mido.MetaMessage("text", text=text))
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def _work(conn, wid="Q1", title="Song", artist="Band", year=1980, rank=1) -> Work:
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, search_artists, work_year, canon_rank, in_pool)"
                 " VALUES (?,?,?,?,?,?,1)", (wid, title, artist, json.dumps([artist]), year, rank))
    conn.commit()
    return Work(wid, title, artist, [artist], year, rank)


def test_select_prefers_the_consensus_medoid(env):
    conn = env
    w = _work(conn)
    good = [midigen.song(bars=40 + k) for k in range(3)]       # < 100 s: identity "good"
    _add(conn, w, "lakh", "g0", good[0])
    _add(conn, w, "midicollection", "g1", good[1])
    _add(conn, w, "freemidi", "g2", good[2])
    # the "bad" transcription gets the best static quality (lmd_matched) but disagrees with the rest
    _add(conn, w, "lakh_clean", "b0", midigen.song(bars=60), lmd_match_score=0.9)
    # a text-only variant of good[0]: different MD5, identical sanitized bytes -> analyzed once
    _add(conn, w, "lakh", "g0dup", _with_text(good[0], "only text differs"))
    conn.execute("INSERT INTO hooktheory_match(work_id, ht_id, title_score, artist_score) VALUES ('Q1','h1',100,100)")
    conn.commit()
    eng = FakeEngine()
    import musichistory.sources.hooktheory as ht
    ht_sections = [{"id": "h1", "annotations": {}}]
    orig = ht.sections_for
    ht.sections_for = lambda c, wid: ht_sections
    try:
        res = sel.select_work(eng, conn, "Q1", "Song", n_analyze=4, workers=2, reanalyze=False)
    finally:
        ht.sections_for = orig
    assert len(eng.analyzed) == 4  # 5 valid candidates, 4 distinct sanitized files
    assert res.chosen is not None and res.chosen.source != "lakh_clean"
    bad = next(c for c in res.analyzed if c.source == "lakh_clean")
    assert bad.quality == max(c.quality for c in res.analyzed)
    assert bad.consensus == pytest.approx(0.05) and bad.hook == 0.2
    goods = [c for c in res.analyzed if c.source != "lakh_clean"]
    assert all(c.consensus == pytest.approx((0.95 * 2 + 0.05) / 3, abs=1e-3) for c in goods)
    row = conn.execute("SELECT candidate_id, n_candidates, n_sources, n_analyzed, reason FROM selection").fetchone()
    assert row["candidate_id"] == res.chosen.candidate_id and row["n_candidates"] == 5
    assert row["n_sources"] == 4 and row["n_analyzed"] == 4 and "cons=" in row["reason"] and "ht=" in row["reason"]
    chosen = conn.execute("SELECT COUNT(*) FROM candidate WHERE chosen=1").fetchone()[0]
    analyzed = conn.execute("SELECT COUNT(*) FROM candidate WHERE analyzed=1").fetchone()[0]
    assert chosen == 1 and analyzed == 4
    # slim analyses are cached: a second run does not call PatternPrep again
    eng2 = FakeEngine()
    sel.select_work(eng2, conn, "Q1", "Song", n_analyze=4, workers=2, reanalyze=False)
    assert eng2.analyzed == []


def test_select_falls_back_when_analysis_fails(env):
    conn = env
    w = _work(conn)
    for k in range(6):
        _add(conn, w, "lakh", f"x{k}", midigen.song(bars=40 + k), lmd_match_score=0.9 if k < 4 else None)
    ok_file = conn.execute("SELECT md5 FROM candidate WHERE source_ref='x5'").fetchone()[0]
    eng = FakeEngine(fail={c[0] for c in conn.execute("SELECT md5 FROM candidate WHERE md5 != ?", (ok_file,))})
    res = sel.select_work(eng, conn, "Q1", "Song", n_analyze=4, workers=1, reanalyze=False)
    assert len(eng.analyzed) == 6
    assert res.chosen is not None
    assert conn.execute("SELECT md5 FROM candidate WHERE chosen=1").fetchone()[0] == ok_file
    errs = conn.execute("SELECT COUNT(*) FROM select_analysis WHERE ok=0").fetchone()[0]
    assert errs == 5


def test_select_no_candidates_and_all_failed(env):
    conn = env
    _work(conn, "Q2", "Nothing", "Nobody")
    res = sel.select_work(FakeEngine(), conn, "Q2", "Nothing", n_analyze=4, workers=1, reanalyze=False)
    assert res.chosen is None and res.reason == "no valid candidates"
    w = _work(conn, "Q3", "Broken", "Band", rank=3)
    _add(conn, w, "lakh", "z", midigen.song(bars=40))
    res = sel.select_work(FakeEngine(fail={"lakh"}), conn, "Q3", "Broken", n_analyze=4, workers=1, reanalyze=False)
    assert res.chosen is None and "analysis failed" in res.reason
    assert conn.execute("SELECT COUNT(*) FROM selection WHERE work_id='Q3'").fetchone()[0] == 0


def test_run_end_to_end_with_report(env):
    conn = env
    for i, year in enumerate([1960, 1961, 1975, 1990]):
        w = _work(conn, f"Q{10 + i}", f"Song {i}", "Band", year=year, rank=i + 1)
        _add(conn, w, "lakh", f"a{i}", midigen.song(bars=40 + i))
        _add(conn, w, "midicollection", f"m{i}", midigen.song(bars=45 + i))
    args = argparse.Namespace(work=None, limit=None, n_analyze=4, workers=2, timeout=10.0, reanalyze=False,
                              target=3, floor=1, floor_first=1960, floor_last=1961, report_only=False)
    assert sel.run(args, engine=FakeEngine()) == 0
    selected = {r[0] for r in conn.execute("SELECT work_id FROM work WHERE selected=1")}
    assert selected == {"Q10", "Q11", "Q12"}
    rep = json.loads((config.DATA / "reports" / "source_comparison.json").read_text())
    assert set(rep["sources"]) == {"lakh", "midicollection"}
    lk = rep["sources"]["lakh"]
    assert lk["candidates"] == 4 and lk["valid"] == 4 and lk["analyzed"] == 4
    assert lk["chosen"] + rep["sources"]["midicollection"]["chosen"] == 4
    assert lk["coverage_by_decade"]["1960"] == {"works_valid": 2, "works_chosen": lk["coverage_by_decade"]["1960"]["works_chosen"], "pool": 2}
    md = (config.DATA / "reports" / "source_comparison.md").read_text()
    assert "| lakh |" in md and "1960s" in md


def test_slim_cache_is_keyed_by_engine_version(env):
    conn = env
    w = _work(conn)
    _add(conn, w, "lakh", "a", midigen.song(bars=40))
    e1 = FakeEngine()
    e1.cache_key = "resonance=aaa"
    sel.select_work(e1, conn, "Q1", "Song", n_analyze=4, workers=1, reanalyze=False)
    e2 = FakeEngine()
    e2.cache_key = "resonance=aaa"
    sel.select_work(e2, conn, "Q1", "Song", n_analyze=4, workers=1, reanalyze=False)
    e3 = FakeEngine()
    e3.cache_key = "resonance=bbb"  # Resonance-2 moved on: analyze again
    sel.select_work(e3, conn, "Q1", "Song", n_analyze=4, workers=1, reanalyze=False)
    assert (len(e1.analyzed), len(e2.analyzed), len(e3.analyzed)) == (1, 0, 1)
