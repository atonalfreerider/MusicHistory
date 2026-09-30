"""The themes stage end to end on a scratch pipeline DB, with a fake classifier and fake lyrics.

Checks resumability (cache key = input SHA-256 + model + backend), the singer fallback, the
graph export, and that the (invented placeholder) lyric text reaches no file at all.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory import config, db  # noqa: E402
from musichistory.themes import classify, lyrics, stage  # noqa: E402

SECRET = "zebra marmalade orbits the teapot"   # invented placeholder "lyric" line


class FakeBackend:
    name = "nli"
    model = "fake@0000000;test"
    calls = 0

    def __init__(self, *a, **k):
        pass

    def load(self):
        pass

    def device_name(self):
        return "cpu"

    def classify(self, items):
        FakeBackend.calls += len(items)
        out = []
        for it in items:
            d = np.full(10, 0.02)
            d[6 if it.lines else 9] = 0.82          # lyrics -> "I miss you", titles -> "Other"
            out.append(d / d.sum())
        return out


def _args(tmp_path: Path, **over) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    stage.add_arguments(p)
    a = p.parse_args(["--out", str(tmp_path / "graph" / "themes_graph.db")])
    for k, v in over.items():
        setattr(a, k, v)
    return a


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_DB", tmp_path / "p.sqlite")
    monkeypatch.setattr(config, "GRAPH_DB", tmp_path / "graph" / "music_graph.db")
    monkeypatch.setattr(config, "GRAPH", tmp_path / "graph")
    monkeypatch.setattr(config, "REPORTS", tmp_path / "reports")
    (tmp_path / "graph").mkdir()
    conn = db.connect()
    for i, (w, sel) in enumerate((("W1", 1), ("W2", 1), ("W3", 2), ("W4", 0)), start=1):
        conn.execute("INSERT INTO work(work_id, title, canonical_artist, work_year, canon_rank, selected)"
                     " VALUES (?,?,?,?,?,?)", (w, f"Title {w}", "Act", 1960 + i, i, sel))
    conn.commit()
    conn.close()
    lines = [SECRET] * 8

    def fake_read(conn, planned, web=True, progress=print):
        st = lyrics.ReadStats(works_planned=1, works_with_lyrics=1)
        return {"W1": lyrics.Lyrics(list(lines), 11, "lakh", "lyric")}, st

    monkeypatch.setattr(lyrics, "read", fake_read)
    monkeypatch.setattr(stage, "make_backend", lambda name, **k: FakeBackend())
    monkeypatch.setattr(stage, "NliClassifier", FakeBackend)
    monkeypatch.setattr(stage.validation, "load", lambda: [
        stage.validation.ValSong("W1", "Title W1", "Act", 7, 2, "tune"),
        stage.validation.ValSong("W2", "Title W2", "Act", 10, None, "holdout")])
    FakeBackend.calls = 0
    return tmp_path


def test_stage_end_to_end_resumable_and_text_free(env, monkeypatch, capsys):
    import builtins
    real_import = builtins.__import__

    def no_singer(name, *a, **k):                      # singer.py may exist in the real tree
        if name.endswith("singer") or (a and a[2] and "singer" in (a[2] or ())):
            raise ImportError("hidden for the test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_singer)
    assert stage.run(_args(env)) == 0
    # W1 lyrics, W2 and W3 titles (W4 is not selected), then 2 validation songs title-only.
    assert FakeBackend.calls == 3 + 2
    conn = sqlite3.connect(env / "p.sqlite")
    rows = {r[0]: r for r in conn.execute("SELECT work_id, text_source, candidate_id, n_lines, backend FROM song_text")}
    assert rows["W1"][1:] == ("lyrics", 11, 8, "nli") and rows["W2"][1] == "title" and "W4" not in rows
    conn.close()
    g = sqlite3.connect(env / "graph" / "themes_graph.db")
    assert g.execute("SELECT COUNT(*) FROM theme_song").fetchone()[0] == 3
    assert {r[0] for r in g.execute("SELECT singer_gender FROM theme_song")} == {"unknown"}
    meta = dict(g.execute("SELECT key, value FROM themes_meta"))
    assert meta["validation_top1"] == "1.0" and meta["lyrics_count"] == "1" and "unavailable" in meta["singer_source"]
    g.close()
    # Second run: every work is cached (same input hash, model and backend).
    FakeBackend.calls = 0
    assert stage.run(_args(env)) == 0
    assert FakeBackend.calls == 2                        # only the validation title-only pass
    # --force re-classifies.
    FakeBackend.calls = 0
    assert stage.run(_args(env, force=True)) == 0
    assert FakeBackend.calls == 3 + 2
    # The lyric text is nowhere: not in any file under the scratch data dir, not in the output.
    out = capsys.readouterr()
    assert SECRET not in out.out and SECRET not in out.err
    needle = SECRET.encode()
    for f in env.rglob("*"):
        if f.is_file():
            assert needle not in f.read_bytes(), f.name
    report = json.loads(sorted((env / "reports").glob("themes_*.json"))[-1].read_text(encoding="utf-8"))
    assert report["text_source_counts"] == {"lyrics": 1, "title": 2} and len(report["examples"]) == 3


def test_resolve_singers_uses_module_when_present(env, monkeypatch):
    import types
    fake = types.ModuleType("musichistory.themes.singer")
    fake.resolve_singers = lambda conn, ids: {w: {"gender": "female", "source": "test", "artist_qid": None,
                                                   "artist_label": None} for w in ids}
    monkeypatch.setitem(sys.modules, "musichistory.themes.singer", fake)
    import musichistory.themes as pkg
    monkeypatch.setattr(pkg, "singer", fake, raising=False)
    conn = db.connect()
    got, src = stage.resolve_singers(conn, ["W1"])
    assert got["W1"]["gender"] == "female" and src == "singer.resolve_singers"
