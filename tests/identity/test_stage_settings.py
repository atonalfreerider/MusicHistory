"""The analyze stage's normalization / TARGET_BPM bookkeeping (no PatternPrep run: the slim is faked)."""

from __future__ import annotations

import argparse
import json

import mido
import pytest
from id_helpers import slim, song_midi

from musichistory import config, db
from musichistory.analysis import patternprep
from musichistory.identity import stage


def e_minor_slim(bars: int = 32) -> dict:
    """i-VI-III-VII in E minor: relative shift +5, parallel shift -4."""
    prog = [(4, "m"), (0, ""), (7, ""), (2, "")]
    chords, notes = [], []
    for b in range(bars):
        root, q = prog[b % 4]
        chords.append([b * 4.0, b * 4.0 + 4, root, q])
        notes += [[b * 4.0 + k, 1.0, 64 + (0, 2, 3, 5)[(b + k) % 4], 3, 3, 1.0] for k in range(4)]
        notes += [[b * 4.0, 4.0, 40 + root, 2, 2, 0.9]]
    return slim(chords, notes, end_beat=bars * 4.0, key_runs=[[0.0, bars * 4.0, 4, True]])


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("DATA", ""), ("SONGS", "songs"), ("NORMALIZED", "normalized"),
                      ("ANALYSIS_WORK", "analysis-work"), ("CANDIDATES", "candidates")):
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(config, "PIPELINE_DB", tmp_path / "musichistory.sqlite")
    monkeypatch.setattr(config, "NORMALIZATION", "relative")
    monkeypatch.setattr(config, "TARGET_BPM", 120.0)
    monkeypatch.setattr(patternprep, "ensure_built", lambda *a, **k: None)
    monkeypatch.setattr(patternprep, "resonance_commit", lambda: "test")
    monkeypatch.setattr(patternprep, "analyze", lambda *a, **k: e_minor_slim())
    conn = db.connect()
    for i, wid in enumerate(("R1", "R2"), 1):
        path = song_midi(tmp_path / "candidates" / wid / "a.mid", transpose=4)
        conn.execute("INSERT INTO work(work_id, title, canonical_artist, canon_rank, selected) VALUES (?,?,?,?,1)",
                     (wid, f"Song {wid}", "Artist", i))
        conn.execute("INSERT INTO candidate(candidate_id, work_id, source, md5, sanitized_path, valid, features_json)"
                     " VALUES (?,?,?,?,?,1,'{}')", (10 + i, wid, "lakh", f"md5{i}", str(path)))
        conn.execute("INSERT INTO selection(work_id, candidate_id) VALUES (?,?)", (wid, 10 + i))
    conn.commit()
    return conn


def args(**kw):
    base = {"limit": None, "work_ids": None, "force": False, "workers": 1, "timeout": 60.0}
    base.update(kw)
    return argparse.Namespace(**base)


def songs(conn):
    return {r["work_id"]: r for r in conn.execute("SELECT * FROM song")}


def test_rows_built_with_other_settings_are_not_done_and_meta_tracks_the_rows(env, capsys):
    """Review 'analyze' (resumable normalization + meta keys): a switched NORMALIZATION or
    TARGET_BPM re-plans the rows built with the old one, and meta analyze_normalization /
    analyze_target_bpm is written only when every analysis_ok row agrees."""
    conn = env
    assert stage.run(args()) == 0
    rows = songs(conn)
    assert {w: (r["normalization"], r["target_bpm"]) for w, r in rows.items()} == {
        "R1": ("relative", 120.0), "R2": ("relative", 120.0)}
    assert conn.execute("SELECT shift FROM key_region WHERE work_id='R1'").fetchone()[0] == 5
    assert db.get_meta(conn, "analyze_normalization") == "relative" and db.get_meta(conn, "analyze_target_bpm") == "120"
    assert stage.run(args()) == 0 and "0 to do, 2 already done" in capsys.readouterr().out

    # Switch to parallel: both rows are stale; a partial run leaves a mixed corpus -> no meta.
    config.NORMALIZATION = "parallel"
    jobs, skipped = stage.plan(conn, work_ids=None, limit=None, force=False, commit="test")
    assert [j["work_id"] for j in jobs] == ["R1", "R2"] and skipped == 0
    stage.run(args(limit=1))
    out = capsys.readouterr().out
    assert "1 to do" in out and "2 analyzed songs were built with another normalization" in out
    assert "WARNING: analyzed songs mix" in out
    assert songs(conn)["R1"]["normalization"] == "parallel" and songs(conn)["R2"]["normalization"] == "relative"
    assert conn.execute("SELECT shift FROM key_region WHERE work_id='R1'").fetchone()[0] == -4
    assert db.get_meta(conn, "analyze_normalization") is None and db.get_meta(conn, "analyze_target_bpm") is None
    # Finishing the switch makes the corpus uniform again.
    stage.run(args())
    assert "1 to do, 1 already done" in capsys.readouterr().out
    assert db.get_meta(conn, "analyze_normalization") == "parallel"

    # A TARGET_BPM change re-plans too, and the normalized MIDI follows it.
    config.TARGET_BPM = 96.0
    assert stage.run(args()) == 0 and "2 to do, 0 already done" in capsys.readouterr().out
    assert {r["target_bpm"] for r in songs(conn).values()} == {96.0}
    assert db.get_meta(conn, "analyze_target_bpm") == "96"
    norm = mido.MidiFile(str(config.NORMALIZED / "R1.mid"))
    assert [m.tempo for tr in norm.tracks for m in tr if m.type == "set_tempo"] == [mido.bpm2tempo(96.0)]


def test_rows_from_before_the_settings_were_recorded_are_redone(env, capsys):
    conn = env
    assert stage.run(args()) == 0
    capsys.readouterr()
    conn.execute("UPDATE song SET normalization = NULL, target_bpm = NULL WHERE work_id = 'R2'")
    conn.commit()
    assert stage.settings_meta(conn) is None and "WARNING" in capsys.readouterr().out
    assert db.get_meta(conn, "analyze_normalization") is None
    jobs, skipped = stage.plan(conn, work_ids=None, limit=None, force=False, commit="test")
    assert [j["work_id"] for j in jobs] == ["R2"] and skipped == 1
    assert stage.run(args()) == 0
    assert db.get_meta(conn, "analyze_normalization") == "relative"


def test_meta_is_cleared_while_a_run_is_under_way(env, monkeypatch):
    conn = env
    assert stage.run(args()) == 0
    config.NORMALIZATION = "parallel"

    def boom(job):
        assert db.get_meta(conn, "analyze_normalization") is None  # not claimed while rows change
        raise KeyboardInterrupt
    monkeypatch.setattr(stage, "process", boom)
    with pytest.raises(KeyboardInterrupt):
        stage.run(args())
    # Nothing changed, so the rows still agree and the meta is restored from them.
    assert db.get_meta(conn, "analyze_normalization") == "relative"


def test_slims_made_before_the_partial_bar_fix_are_not_reused(tmp_path):
    """Review 'analyze' (bar grid): cached slims with merged measure runs must be recomputed."""
    s = e_minor_slim()
    p = tmp_path / "slim.json"
    s["slim_version"] = 1
    p.write_text(json.dumps(s), encoding="utf-8")
    assert stage._reusable([str(p)], "test", None) is None
    s["slim_version"] = patternprep.SLIM_VERSION
    p.write_text(json.dumps(s), encoding="utf-8")
    assert stage._reusable([str(p)], "test", None) is not None
