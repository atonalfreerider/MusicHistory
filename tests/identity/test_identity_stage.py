"""The analyze stage end to end on an isolated pipeline DB (runs PatternPrep, 2 workers)."""

from __future__ import annotations

import argparse
import json

import mido
import pytest
from id_helpers import song_midi

from musichistory import config, db
from musichistory.analysis import patternprep
from musichistory.identity import stage


@pytest.fixture
def data(tmp_path, monkeypatch):
    for name, sub in (("DATA", ""), ("SONGS", "songs"), ("NORMALIZED", "normalized"),
                      ("ANALYSIS_WORK", "analysis-work"), ("CANDIDATES", "candidates")):
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(config, "PIPELINE_DB", tmp_path / "musichistory.sqlite")
    return tmp_path


def _add_work(conn, wid, cid, path, rank, features=None, selected=1):
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, canon_rank, selected) VALUES (?,?,?,?,?)",
                 (wid, f"Song {wid}", "Artist", rank, selected))
    conn.execute("INSERT INTO candidate(candidate_id, work_id, source, md5, sanitized_path, valid, features_json)"
                 " VALUES (?,?,?,?,?,1,?)", (cid, wid, "lakh", f"md5{cid}", str(path), json.dumps(features or {})))
    conn.execute("INSERT INTO selection(work_id, candidate_id) VALUES (?,?)", (wid, cid))


def args(**kw):
    base = {"limit": None, "work_ids": None, "force": False, "workers": 2, "timeout": 120.0}
    base.update(kw)
    return argparse.Namespace(**base)


def test_stage_end_to_end(data, capsys):
    conn = db.connect()
    a = song_midi(data / "candidates" / "R1" / "a.mid")
    b = song_midi(data / "candidates" / "R2" / "b.mid", transpose=2, bpm=90.0)
    bad = data / "candidates" / "R3" / "c.mid"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0MTrk\x00\x00\x00\x40\x00\x90\x3c")
    feats = {"tracks": [{"index": 3, "role": "melody", "role_src": "name", "is_drum": False, "n_notes": 128}],
             "key_signatures": [[0.0, 2, 0]]}
    _add_work(conn, "R1", 11, a, 1, feats)
    _add_work(conn, "R2", 12, b, 2)
    _add_work(conn, "R3", 13, bad, 3)
    _add_work(conn, "R4", 14, a, 4, selected=0)   # not selected: skipped
    conn.commit()
    # A select-time slim for R2 is reused instead of re-running PatternPrep.
    select_dir = config.ANALYSIS_WORK / "R2" / "c12"
    select_slim = patternprep.analyze(b, select_dir)
    (select_dir / "slim.json").write_text(json.dumps(select_slim), encoding="utf-8")

    assert stage.run(args()) == 0
    out = capsys.readouterr().out
    assert "3 to do" in out and "reused" in out and "R3 FAILED" in out
    rows = {r["work_id"]: r for r in conn.execute("SELECT * FROM song")}
    assert set(rows) == {"R1", "R2", "R3"}
    r1, r2, r3 = rows["R1"], rows["R2"], rows["R3"]
    assert r1["analysis_ok"] == 1 and r1["tonic_pc"] == 0 and r1["mode"] == "major" and r1["melody_method"] == "name"
    assert r2["analysis_ok"] == 1 and r2["tonic_pc"] == 2 and r2["norm_shift"] == -2 and r2["native_bpm"] == 90.0
    assert r3["analysis_ok"] == 0 and "PatternPrep exit" in r3["error"]
    assert r1["midi_path"].endswith("songs/R1/score.mid") and r1["patterns_path"].endswith("songs/R1/analysis.json")
    for wid in ("R1", "R2"):
        assert (config.SONGS / wid / "score.mid").exists() and (config.SONGS / wid / "analysis.json").exists()
        norm = mido.MidiFile(str(config.NORMALIZED / f"{wid}.mid"))
        tempos = [m.tempo for tr in norm.tracks for m in tr if m.type == "set_tempo"]
        assert tempos == [mido.bpm2tempo(config.TARGET_BPM)]
    # Both songs normalize to the same identity (same progression and tune, different key and tempo).
    tok = {w: json.loads(conn.execute("SELECT tokens FROM chord_seq WHERE work_id=? AND kind='chg' AND level='L1'",
                                      (w,)).fetchone()[0]) for w in ("R1", "R2")}
    assert tok["R1"] == tok["R2"]
    mel = {w: json.loads(conn.execute("SELECT pitches FROM melody_line WHERE work_id=? AND role='melody'",
                                      (w,)).fetchone()[0]) for w in ("R1", "R2")}
    assert mel["R1"] == mel["R2"]
    assert conn.execute("SELECT COUNT(*) FROM chord_seq WHERE work_id='R3'").fetchone()[0] == 0

    # Resumable: nothing to do the second time, except the failed work.
    assert stage.run(args()) == 1  # only the broken file is retried, and it fails again
    assert "1 to do, 2 already done" in capsys.readouterr().out
    assert stage.run(args(force=True, work_ids=["R1"], workers=1)) == 0
    assert "1 to do" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM song WHERE analysis_ok=1").fetchone()[0] == 2
