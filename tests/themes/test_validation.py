"""The hand-labelled validation set and the accuracy metrics."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.themes import validation as V  # noqa: E402

ALLOWED_KEYS = {"work_id", "title", "artist", "primary", "secondary", "split", "instrumental"}


def test_validation_set_shape():
    doc = json.loads(V.VALIDATION_SET.read_text(encoding="utf-8"))
    songs = doc["songs"]
    assert 45 <= len(songs) <= 80
    for s in songs:
        assert set(s) <= ALLOWED_KEYS                      # titles/artists/theme ids only, no text fields
        assert 1 <= s["primary"] <= 10 and (s["secondary"] is None or 1 <= s["secondary"] <= 10)
        assert s["secondary"] != s["primary"] and s["split"] in ("tune", "holdout")
        if s.get("instrumental"):
            assert s["primary"] == 10
    per_theme = Counter(s["primary"] for s in songs)
    assert set(per_theme) == set(range(1, 11)) and min(per_theme.values()) >= 5
    hold = sum(s["split"] == "holdout" for s in songs) / len(songs)
    assert 0.28 <= hold <= 0.40
    assert len({s["work_id"] for s in songs}) == len(songs)


@pytest.mark.skipif(not (ROOT / "data" / "graph" / "music_graph.db").exists(), reason="no music graph")
def test_validation_songs_are_in_the_graph():
    g = sqlite3.connect(f"file:{(ROOT / 'data' / 'graph' / 'music_graph.db').as_posix()}?mode=ro", uri=True)
    ids = {r[0] for r in g.execute("SELECT work_id FROM song_node")}
    g.close()
    assert all(s.work_id in ids for s in V.load())


def _d(order: list[int]) -> np.ndarray:
    d = np.zeros(10)
    for rank, k in enumerate(order):
        d[k - 1] = 10 - rank
    return d / d.sum()


def test_metrics():
    songs = [V.ValSong("A", "a", "x", 3, 1, "tune"), V.ValSong("B", "b", "x", 7, 2, "holdout"),
             V.ValSong("C", "c", "x", 10, None, "tune"), V.ValSong("D", "d", "x", 5, None, "tune")]
    dists = {"A": _d([3, 1]), "B": _d([2, 7]), "C": _d([4, 1, 10])}
    r = V.score(dists, songs)
    assert r == {"n": 3, "top1": round(1 / 3, 3), "top1_ok": round(2 / 3, 3), "top2": round(2 / 3, 3),
                 "mean_rank": round((1 + 2 + 3) / 3, 2)}
    rep = V.report(dists, {"A": "lyrics", "B": "lyrics", "C": "title"}, songs)
    assert rep["lyrics"]["n"] == 2 and rep["title"]["n"] == 1 and rep["holdout"]["n"] == 1
    assert V.confusion(dists, songs) == {3: {3: 1}, 7: {2: 1}, 10: {4: 1}}
