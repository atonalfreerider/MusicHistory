"""Pipeline tables and the themes graph database (schema checked against DESIGN.md §12)."""

from __future__ import annotations

import math
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory import db  # noqa: E402
from musichistory.themes import export  # noqa: E402


def _design_columns() -> dict[str, list[str]]:
    text = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    sec = text[text.index("**Themes graph DB**"):]
    sql = sec[sec.index("```sql") + 6:sec.index("```", sec.index("```sql") + 6)]
    sql = re.sub(r"--[^\n]*", "", sql)
    out = {}
    for name, body in re.findall(r"CREATE TABLE (\w+)\((.*?)\);", sql, re.S):
        cols = []
        for part in re.split(r",(?![^()]*\))", body):
            w = part.strip().split()
            if w and w[0].upper() not in ("PRIMARY", "UNIQUE", "CHECK"):
                cols.append(w[0])
        out[name] = cols
    return out


def _music_graph(path: Path) -> None:
    g = sqlite3.connect(path)
    g.execute("CREATE TABLE song_node(node_id INTEGER PRIMARY KEY, work_id TEXT, title TEXT, artist TEXT, year INTEGER,"
              " midi_path TEXT, excerpt_start_beat REAL, excerpt_end_beat REAL, tonic_pc INTEGER, mode TEXT,"
              " native_bpm REAL, beats_per_bar REAL, first_downbeat REAL)")
    g.executemany("INSERT INTO song_node VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1, "W1", "Graph Title One", "Act A", 1970, "../songs/W1/score.mid", 8.0, 40.0, 0, "major", 120.0, 4.0, 0.0),
        (2, "W2", "Graph Title Two", "Act B", 1965, "../songs/W2/score.mid", 4.0, 36.0, 9, "minor", 96.5, 3.0, 1.0),
    ])
    g.commit()
    g.close()


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "p.sqlite")
    export.ensure_schema(c)
    for w, t, y in (("W1", "Work One", 1970), ("W2", "Work Two", 1965), ("W3", "Work Three", 1965)):
        c.execute("INSERT INTO work(work_id, title, canonical_artist, work_year, selected) VALUES (?,?,?,?,1)",
                  (w, t, "Someone", y))
    c.commit()
    return c


def _dist(top: int, p: float = 0.7) -> np.ndarray:
    d = np.full(10, (1 - p) / 9)
    d[top - 1] = p
    return d


def test_write_song_normalizes_and_replaces(conn):
    export.write_song(conn, "W1", text_source="lyrics", candidate_id=5, text_sha256="ab" * 32, n_lines=40,
                      model="m", backend="nli", dist=np.arange(1, 11, dtype=float))
    export.write_song(conn, "W1", text_source="lyrics", candidate_id=5, text_sha256="ab" * 32, n_lines=40,
                      model="m", backend="nli", dist=_dist(3))
    d = export.stored_distributions(conn)["W1"]
    assert abs(d.sum() - 1) < 1e-9 and int(np.argmax(d)) == 2
    assert conn.execute("SELECT COUNT(*) FROM song_theme").fetchone()[0] == 10
    cols = [r[1] for r in conn.execute("PRAGMA table_info(song_text)")]
    assert cols == ["work_id", "text_source", "candidate_id", "text_sha256", "n_lines", "model", "backend",
                    "classified_at"]
    with pytest.raises(ValueError):
        export.write_song(conn, "W2", text_source="title", candidate_id=None, text_sha256="x", n_lines=0,
                          model="m", backend="nli", dist=np.ones(9))


def test_export_graph_matches_design(conn, tmp_path):
    music = tmp_path / "music_graph.db"
    _music_graph(music)
    for w, top, src in (("W1", 3, "lyrics"), ("W2", 7, "title"), ("W3", 10, "title")):
        export.write_song(conn, w, text_source=src, candidate_id=1 if src == "lyrics" else None,
                          text_sha256="0" * 64, n_lines=30 if src == "lyrics" else 0, model="m", backend="nli",
                          dist=_dist(top))
    conn.commit()
    out = tmp_path / "graph" / "themes_graph.db"
    summary = export.export_graph(conn, ["W1", "W2", "W3"], backend="nli", model="m",
                                  singers={"W1": {"gender": "female"}, "W2": {"gender": "robot"}},
                                  meta={"validation_top1": 0.5, "validation_top2": 0.75}, out=out, music_db=music)
    assert summary["songs"] == 3 and summary["by_source"] == {"lyrics": 1, "title": 2}
    g = sqlite3.connect(out)
    g.row_factory = sqlite3.Row
    assert g.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    design = _design_columns()
    assert set(design) == {"themes_meta", "theme_anchor", "theme_song", "theme_score", "themes_layout_run"}
    for table, cols in design.items():
        assert [r[1] for r in g.execute(f"PRAGMA table_info({table})")] == cols, table
    # anchors: radius 40, 36 degrees apart in the x-z plane
    anchors = g.execute("SELECT * FROM theme_anchor ORDER BY anchor_id").fetchall()
    assert [a["anchor_id"] for a in anchors] == list(range(1, 11)) and anchors[2]["label"] == "I love you/him/her"
    for a in anchors:
        assert a["angle"] == pytest.approx(36.0 * (a["anchor_id"] - 1)) and a["position_y"] == 0
        assert math.hypot(a["position_x"], a["position_z"]) == pytest.approx(40.0)
    # songs: node ids by (year, work_id); graph titles and playback copied; positions NULL
    songs = g.execute("SELECT * FROM theme_song ORDER BY node_id").fetchall()
    assert [s["work_id"] for s in songs] == ["W2", "W3", "W1"]
    w2, w3, w1 = songs
    assert w2["title"] == "Graph Title Two" and w2["midi_path"] == "../songs/W2/score.mid" and w2["mode"] == "minor"
    assert w3["title"] == "Work Three" and w3["midi_path"] is None and w3["year"] == 1965
    assert (w1["singer_gender"], w2["singer_gender"], w3["singer_gender"]) == ("female", "unknown", "unknown")
    assert (w1["top_anchor"], w2["top_anchor"], w3["top_anchor"]) == (3, 7, 10)
    assert w1["top_score"] == pytest.approx(0.7) and w1["text_source"] == "lyrics"
    assert all(s["position_x"] is None and s["position_y"] is None and s["position_z"] is None for s in songs)
    sums = [r[0] for r in g.execute("SELECT SUM(score) FROM theme_score GROUP BY node_id")]
    assert len(sums) == 3 and all(abs(s - 1) < 1e-9 for s in sums)
    meta = dict(g.execute("SELECT key, value FROM themes_meta"))
    for k in ("backend", "model", "generated_at", "song_count", "lyrics_count", "title_count", "ring_radius",
              "validation_top1", "validation_top2"):
        assert k in meta, k
    assert (meta["song_count"], meta["lyrics_count"], meta["title_count"], float(meta["ring_radius"])) == ("3", "1", "2", 40.0)
    assert g.execute("SELECT COUNT(*) FROM themes_layout_run").fetchone()[0] == 0
    g.close()


def test_export_refuses_unscored_works(conn, tmp_path):
    with pytest.raises(RuntimeError):
        export.export_graph(conn, ["W1"], backend="nli", model="m", singers={}, out=tmp_path / "t.db",
                            music_db=tmp_path / "missing.db")
