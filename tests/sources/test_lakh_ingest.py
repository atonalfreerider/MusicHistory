"""Lakh index/search/extraction on a miniature fake LMD cache, and candidate ingestion."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "midi"))

import midigen  # noqa: E402
from musichistory import config, db  # noqa: E402
from musichistory.sources import base, hooktheory, lakh  # noqa: E402
from musichistory.sources.base import Hit, Work  # noqa: E402


def _variant(k: int) -> bytes:
    tracks = [
        {"name": "Vocal", "channel": 0, "program": 52, "pitches": [60 + k, 62, 64], "step": 1.0},
        {"name": "Bass", "channel": 1, "program": 33, "pitches": [36, 43], "step": 2.0},
        {"name": "Guitar", "channel": 2, "program": 25, "pitches": [52, 55, 59], "step": 1.0},
        {"name": "Strings", "channel": 3, "program": 48, "pitches": [60, 64, 67], "step": 4.0, "chord": True},
        {"name": "Drums", "channel": 9, "program": 0, "pitches": [36, 38], "step": 1.0},
    ]
    return midigen.song(bars=30 + k, tracks=tracks)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fake LMD cache + scratch pipeline DB; returns (conn, files by label)."""
    files = {k: _variant(i) for i, k in enumerate(["u2", "u2_prob", "u2_title_only", "u2_cover", "mariah", "enrique",
                                                   "clean_only", "u2_short"])}
    files["u2_short"] = midigen.song(bars=8)  # too short: stored as an invalid candidate
    md5 = {k: hashlib.md5(v).hexdigest() for k, v in files.items()}
    cache = tmp_path / "cache"
    ld = cache / "lakh"
    ld.mkdir(parents=True)
    paths = {
        md5["u2"]: ["U2/With or Without You.mid", "w/withorwithoutyou2.mid"],
        md5["u2_prob"]: ["w/withorwithoutyou.mid"],
        md5["u2_title_only"]: ["Various/With or Without You.mid"],
        md5["u2_cover"]: ["Cover Band/With or Without You.mid"],
        md5["mariah"]: ["Mariah Carey/Hero.mid", "h/hero.mid"],
        md5["enrique"]: ["Enrique Iglesias/Hero.mid"],
        md5["u2_short"]: ["U2/With or Without You (short).mid"],
    }
    (ld / "md5_to_paths.json").write_text(json.dumps(paths))
    (ld / "match_scores.json").write_text(json.dumps({
        "TRAAAAA12345678901": {md5["u2_prob"]: 0.81, md5["u2"]: 0.66},
        "TRBBBBB12345678901": {md5["u2_title_only"]: 0.9},
    }))
    (ld / "unique_tracks.txt").write_text(
        "TRAAAAA12345678901<SEP>SOA<SEP>U2<SEP>With Or Without You\n"
        "TRBBBBB12345678901<SEP>SOB<SEP>Some Band<SEP>Another Song\n", encoding="utf-8")
    with tarfile.open(ld / "lmd_full.tar.gz", "w:gz") as tf:
        for k, data in files.items():
            if k == "clean_only":
                continue
            ti = tarfile.TarInfo(f"lmd_full/{md5[k][0]}/{md5[k]}.mid")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    with tarfile.open(ld / "clean_midi.tar.gz", "w:gz") as tf:
        for name, k in [("clean_midi/U2/With or Without You.mid", "u2"),
                        ("clean_midi/U2/With or Without You.1.mid", "clean_only")]:
            ti = tarfile.TarInfo(name)
            ti.size = len(files[k])
            tf.addfile(ti, io.BytesIO(files[k]))
    monkeypatch.setattr(config, "CACHE", cache)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "CANDIDATES", tmp_path / "data" / "candidates")
    conn = db.connect(tmp_path / "data" / "musichistory.sqlite")
    base.ensure_schema(conn)
    return conn, files, md5


WOWY = Work("Q1131812", "With or Without You", "U2", ["U2"], 1987, 143)
HERO = Work("Q2545027", "Hero", "Enrique Iglesias", ["Enrique Iglesias"], 2001, 508)


def test_index_search_classes(env):
    conn, files, md5 = env
    idx = lakh.LakhIndex.open()
    meta = idx.meta()
    assert meta["n_clean"] == "2" and meta["n_msd"] == "2"
    hits, rejected = idx.search(WOWY)
    got = {h.source_ref: (h.source, h.match_class) for s in hits.values() for h in s}
    assert got[md5["u2"]] == ("lakh_clean", "accept")          # clean_midi path preferred
    assert got[md5["clean_only"]] == ("lakh_clean", "accept")  # only in clean_midi
    assert got[md5["u2_prob"]] == ("lakh", "probable")         # title + lmd_matched MSD U2 track
    assert md5["u2_title_only"] not in got                     # lmd_matched, but to another song
    assert md5["u2_cover"] not in got                          # same title, other artist
    assert rejected == 2
    prob = next(h for h in hits["lakh"] if h.source_ref == md5["u2_prob"])
    assert prob.lmd_match_score == 0.81 and prob.lmd_msd_id == "TRAAAAA12345678901"
    acc = next(h for h in hits["lakh_clean"] if h.source_ref == md5["u2"])
    assert acc.lmd_match_score == 0.66
    h2, _ = idx.search(HERO)
    assert {h.source_ref for s in h2.values() for h in s} == {md5["enrique"]}


def test_extract_streams_both_archives(env):
    conn, files, md5 = env
    idx = lakh.LakhIndex.open()
    want = {md5["u2"], md5["clean_only"], md5["enrique"]}
    got = dict(idx.extract(want))
    assert set(got) == want
    assert got[md5["clean_only"]] == files["clean_only"]
    assert hashlib.md5(got[md5["u2"]]).hexdigest() == md5["u2"]


def test_fetch_ingest_and_dedupe(env):
    conn, files, md5 = env
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, search_artists, work_year, canon_rank, in_pool)"
                 " VALUES (?,?,?,?,?,?,1)", ("Q1131812", "With or Without You", "U2", '["U2"]', 1987, 143))
    conn.commit()
    works = base.load_works(conn)
    stats = {s.source: s for s in lakh.fetch(conn, works)}
    assert stats["lakh_clean"].valid == 2 and stats["lakh"].valid == 1 and stats["lakh"].invalid == 1
    rows = conn.execute("SELECT source, md5, valid, invalid_reason, sanitized_path, sha256, match_class"
                        " FROM candidate ORDER BY source, md5").fetchall()
    assert len(rows) == 4
    short = [r for r in rows if r["md5"] == md5["u2_short"]][0]
    assert short["valid"] == 0 and short["invalid_reason"] == "too_short" and short["sanitized_path"] is None
    for r in rows:
        if r["valid"]:
            p = base.resolve(r["sanitized_path"])
            assert p.exists() and p.name == f"{r['source']}__{r['md5']}.mid"
            assert r["sanitized_path"].startswith("data/candidates/Q1131812/")
            assert hashlib.sha256(p.read_bytes()).hexdigest() == r["sha256"]
    # the same original bytes from a web source are a duplicate, not a second candidate
    w = works[0]
    hit = Hit("midicollection", "999", "u2-with-or-without-you", 100, 100, "accept")
    assert base.ingest(conn, w, hit, files["u2"]) == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 4
    assert base.ingest(conn, w, Hit("freemidi", "1", "x", 100, 100, "accept"), b"<html>oops</html>") == "invalid"
    assert conn.execute("SELECT invalid_reason FROM candidate WHERE source='freemidi'").fetchone()[0] == "html"
    # a second run is a no-op (resumable)
    again = {s.source: s for s in lakh.fetch(conn, works)}
    assert again["lakh"].valid == again["lakh_clean"].valid == again["lakh"].invalid == 0
    assert conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 5


def test_squashed_ing_paths_are_found(env):
    """Review 'fetch-midi' #3: 'DancingQueen3' squashes to 'dancinqueen3' in the index, like the title."""
    conn, files, md5 = env
    ld = config.CACHE / "lakh"
    paths = json.loads((ld / "md5_to_paths.json").read_text())
    paths["f" * 32] = ["Abba/DancingQueen3.mid"]
    paths["e" * 32] = ["b/beegeesstayingalive.mid"]
    (ld / "md5_to_paths.json").write_text(json.dumps(paths))
    idx = lakh.LakhIndex.open()
    assert idx.meta()["version"] == lakh.INDEX_VERSION
    hits, _ = idx.search(Work("Q1", "Dancing Queen", "ABBA", ["ABBA"]))
    assert [h.source_ref for h in hits["lakh"]] == ["f" * 32]
    hits, _ = idx.search(Work("Q2", "Stayin' Alive", "Bee Gees", ["Bee Gees"]))
    assert [h.source_ref for h in hits["lakh"]] == ["e" * 32]


def test_resanitize_updates_every_stored_row_in_place(env):
    """--resanitize keeps candidate_ids (rows updated, not deleted and re-inserted) and also
    re-sanitizes stored candidates that are no longer among the top hits."""
    conn, files, md5 = env
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, search_artists, work_year, canon_rank, in_pool)"
                 " VALUES (?,?,?,?,?,?,1)", ("Q1131812", "With or Without You", "U2", '["U2"]', 1987, 143))
    conn.commit()
    works = base.load_works(conn)
    lakh.fetch(conn, works)
    before = {r["md5"]: tuple(r) for r in conn.execute(
        "SELECT md5, candidate_id, source, valid, sha256, sanitized_path FROM candidate")}
    assert len(before) == 4
    conn.execute("UPDATE candidate SET sha256='stale', features_json=NULL, fetched_at=NULL")
    conn.commit()
    stats = {s.source: s for s in lakh.fetch(conn, works, resanitize=True, max_per_source=1)}
    after = {r["md5"]: tuple(r) for r in conn.execute(
        "SELECT md5, candidate_id, source, valid, sha256, sanitized_path FROM candidate")}
    assert after == before  # same ids, labels, validity and files; every stale sha256 rewritten
    assert conn.execute("SELECT COUNT(*) FROM candidate WHERE fetched_at IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidate WHERE valid=1 AND features_json IS NULL").fetchone()[0] == 0
    assert stats["lakh_clean"].valid == 2 and stats["lakh"].valid == 1 and stats["lakh"].invalid == 1


def test_hooktheory_matching(env, monkeypatch):
    conn, *_ = env
    fake = {
        "a1": {"hooktheory": {"artist": "u2", "song": "with-or-without-you"}, "annotations": {}},
        "a2": {"hooktheory": {"artist": "u2", "song": "with-or-without-you"}, "annotations": {}},
        "b1": {"hooktheory": {"artist": "mariah-carey", "song": "hero"}, "annotations": {}},
    }
    monkeypatch.setattr(hooktheory, "load", lambda offline=True: fake)
    n_works, n_sections = hooktheory.match_works(conn, [WOWY, HERO])
    assert (n_works, n_sections) == (1, 2)
    secs = hooktheory.sections_for(conn, "Q1131812")
    assert [s["id"] for s in secs] == ["a1", "a2"]
    assert hooktheory.sections_for(conn, "Q2545027") == []


def test_hooktheory_file_hash_is_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE", tmp_path)
    p = tmp_path / "hooktheory" / "Hooktheory.json.gz"
    p.parent.mkdir(parents=True)
    p.write_bytes(gzip.compress(b"{}"))
    with pytest.raises(ValueError):
        hooktheory.ensure_file(offline=True)
    assert not p.exists()


def test_real_hooktheory_cache_hash():
    real = ROOT / "data" / "cache" / "hooktheory" / "Hooktheory.json.gz"
    if not real.exists():
        pytest.skip("Hooktheory.json.gz not in data/cache")
    assert hashlib.sha256(real.read_bytes()).hexdigest() == hooktheory.SHA256
