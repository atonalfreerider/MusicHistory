"""Pipeline tables of the themes stage and the themes graph database (DESIGN.md §12).

Pipeline tables (in ``data/musichistory.sqlite``, created here with IF NOT EXISTS):

    song_text(work_id PK, text_source, candidate_id, text_sha256, n_lines, model, backend, classified_at)
    song_theme(work_id, anchor_id, score, PK(work_id, anchor_id))      -- rows sum to 1 per work

``song_text`` never holds text: ``text_sha256`` is the SHA-256 of the classifier input (the
lyric lines plus the title, or the title alone) and, with ``model`` and ``backend``, is the
cache key that makes the stage resumable. ``n_lines`` is 0 for title-only songs. The
``singer`` table belongs to ``singer.py``.

Themes graph (``data/graph/themes_graph.db``), SQLite 3.15-compatible (no STRICT, no
generated columns, no window functions or UPSERT), ``journal_mode=DELETE``; rebuilt from
scratch on every export (song positions are NULL until ``MusicHistory.Layout.exe themes``
runs). Anchors sit on a ring of radius ``RING_RADIUS`` in the x-z plane at angle
``36 deg * (k - 1)`` (``theme_anchor.angle`` in degrees), y = 0. Songs are node ids 1..N by
(year, work_id). Playback columns are copied from the music graph's ``song_node``;
``midi_path`` stays relative to the graph folder (both databases live in ``data/graph``).
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
from pathlib import Path

import numpy as np

from .. import config
from .classify import N_THEMES, THEMES

RING_RADIUS = 40.0
SCHEMA_VERSION = "1"


def themes_graph_db() -> Path:
    return config.GRAPH / "themes_graph.db"

PIPELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS song_text(
  work_id TEXT PRIMARY KEY,
  text_source TEXT NOT NULL,         -- 'lyrics' | 'title'
  candidate_id INTEGER,              -- lyric source candidate (NULL for title)
  text_sha256 TEXT NOT NULL,         -- SHA-256 of the classifier input; never the text itself
  n_lines INTEGER NOT NULL,          -- lyric lines (0 for title)
  model TEXT NOT NULL,               -- checkpoint@revision;settings (or the Claude model;prompt)
  backend TEXT NOT NULL,             -- 'nli' | 'claude'
  classified_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS song_theme(
  work_id TEXT NOT NULL, anchor_id INTEGER NOT NULL, score REAL NOT NULL,
  PRIMARY KEY(work_id, anchor_id));
"""

GRAPH_SCHEMA = """
CREATE TABLE themes_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE theme_anchor(anchor_id INTEGER PRIMARY KEY, label TEXT NOT NULL, short TEXT NOT NULL,
  angle REAL NOT NULL, position_x REAL NOT NULL, position_y REAL NOT NULL, position_z REAL NOT NULL);
CREATE TABLE theme_song(node_id INTEGER PRIMARY KEY,
  work_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, artist TEXT NOT NULL, year INTEGER NOT NULL,
  singer_gender TEXT NOT NULL, text_source TEXT NOT NULL,
  top_anchor INTEGER NOT NULL, top_score REAL NOT NULL,
  position_x REAL, position_y REAL, position_z REAL,
  midi_path TEXT, excerpt_start_beat REAL, excerpt_end_beat REAL, tonic_pc INTEGER, mode TEXT,
  native_bpm REAL, beats_per_bar REAL, first_downbeat REAL);
CREATE TABLE theme_score(node_id INTEGER NOT NULL, anchor_id INTEGER NOT NULL, score REAL NOT NULL,
  PRIMARY KEY(node_id, anchor_id));
CREATE TABLE themes_layout_run(run_id INTEGER PRIMARY KEY, created_at TEXT, device TEXT,
  iterations INTEGER, sharpen REAL, repulsion REAL, final_mean_move REAL, params_json TEXT);
"""

SINGER_GENDERS = ("male", "female", "mixed", "nonbinary", "unknown", "instrumental")


# ------------------------------------------------------------------ pipeline tables
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(PIPELINE_SCHEMA)
    conn.commit()


def cached(conn: sqlite3.Connection, work_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM song_text WHERE work_id=?", (work_id,)).fetchone()


def write_song(conn: sqlite3.Connection, work_id: str, *, text_source: str, candidate_id: int | None,
               text_sha256: str, n_lines: int, model: str, backend: str, dist: np.ndarray) -> None:
    d = np.asarray(dist, dtype=np.float64)
    if d.shape != (N_THEMES,) or not np.all(np.isfinite(d)):
        raise ValueError(f"bad theme distribution for {work_id}")
    d = np.clip(d, 0.0, None)
    d = d / d.sum()
    conn.execute(
        "INSERT OR REPLACE INTO song_text(work_id, text_source, candidate_id, text_sha256, n_lines, model, backend,"
        " classified_at) VALUES (?,?,?,?,?,?,?,?)",
        (work_id, text_source, candidate_id, text_sha256, int(n_lines), model, backend,
         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    conn.execute("DELETE FROM song_theme WHERE work_id=?", (work_id,))
    conn.executemany("INSERT INTO song_theme(work_id, anchor_id, score) VALUES (?,?,?)",
                     [(work_id, t.id, float(d[i])) for i, t in enumerate(THEMES)])


def stored_distributions(conn: sqlite3.Connection, work_ids: list[str] | None = None) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for wid, aid, sc in conn.execute("SELECT work_id, anchor_id, score FROM song_theme"):
        if work_ids is not None and wid not in work_ids:
            continue
        out.setdefault(wid, np.zeros(N_THEMES))[aid - 1] = sc
    return out


# ------------------------------------------------------------------ graph export
def anchor_rows(radius: float = RING_RADIUS) -> list[tuple]:
    rows = []
    for t in THEMES:
        angle = 36.0 * (t.id - 1)
        rad = math.radians(angle)
        x, z = radius * math.cos(rad), radius * math.sin(rad)
        rows.append((t.id, t.label, t.short, angle, round(x, 6), 0.0, round(z, 6)))
    return rows


def _music_nodes(music_db: Path) -> dict[str, sqlite3.Row]:
    if not music_db.exists():
        return {}
    g = sqlite3.connect(f"file:{music_db.as_posix()}?mode=ro", uri=True)
    g.row_factory = sqlite3.Row
    try:
        return {r["work_id"]: r for r in g.execute(
            "SELECT work_id, title, artist, year, midi_path, excerpt_start_beat, excerpt_end_beat, tonic_pc, mode,"
            " native_bpm, beats_per_bar, first_downbeat FROM song_node")}
    finally:
        g.close()


def export_graph(conn: sqlite3.Connection, work_ids: list[str], *, backend: str, model: str,
                 singers: dict[str, dict], meta: dict[str, str] | None = None,
                 out: Path | None = None, music_db: Path | None = None,
                 radius: float = RING_RADIUS) -> dict:
    """Write the themes graph for ``work_ids`` (every one must have scores). Returns a summary."""
    out = Path(out) if out else themes_graph_db()
    music_db = Path(music_db) if music_db else config.GRAPH_DB
    dists = stored_distributions(conn, set(work_ids))
    missing = [w for w in work_ids if w not in dists]
    if missing:
        raise RuntimeError(f"{len(missing)} works have no theme scores (first: {missing[0]})")
    texts = {r["work_id"]: r for r in conn.execute("SELECT work_id, text_source FROM song_text")}
    works = {r["work_id"]: r for r in conn.execute("SELECT work_id, title, canonical_artist, work_year FROM work")}
    nodes = _music_nodes(music_db)
    songs = []
    for w in work_ids:
        n, wk = nodes.get(w), works.get(w)
        year = n["year"] if n is not None else (wk["work_year"] if wk is not None else None)
        title = n["title"] if n is not None else (wk["title"] if wk is not None else w)
        artist = n["artist"] if n is not None else (wk["canonical_artist"] if wk is not None else "")
        songs.append((int(year or 0), w, title, artist, n))
    songs.sort(key=lambda s: (s[0], s[1]))

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".building")
    tmp.unlink(missing_ok=True)
    g = sqlite3.connect(tmp)
    g.execute("PRAGMA journal_mode=DELETE")
    g.executescript(GRAPH_SCHEMA)
    g.executemany("INSERT INTO theme_anchor VALUES (?,?,?,?,?,?,?)", anchor_rows(radius))
    counts = {"lyrics": 0, "title": 0}
    top_counts = {t.id: 0 for t in THEMES}
    gender_counts: dict[str, int] = {}
    for node_id, (year, w, title, artist, n) in enumerate(songs, start=1):
        d = dists[w] / dists[w].sum()
        top = int(np.argmax(d)) + 1
        src = texts[w]["text_source"] if w in texts else "title"
        counts[src] = counts.get(src, 0) + 1
        top_counts[top] += 1
        gender = (singers.get(w) or {}).get("gender") or "unknown"
        if gender not in SINGER_GENDERS:
            gender = "unknown"
        gender_counts[gender] = gender_counts.get(gender, 0) + 1
        play = (n["midi_path"], n["excerpt_start_beat"], n["excerpt_end_beat"], n["tonic_pc"], n["mode"],
                n["native_bpm"], n["beats_per_bar"], n["first_downbeat"]) if n is not None else (None,) * 8
        g.execute("INSERT INTO theme_song(node_id, work_id, title, artist, year, singer_gender, text_source, top_anchor,"
                  " top_score, position_x, position_y, position_z, midi_path, excerpt_start_beat, excerpt_end_beat,"
                  " tonic_pc, mode, native_bpm, beats_per_bar, first_downbeat)"
                  " VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,?)",
                  (node_id, w, title, artist, year, gender, src, top, float(d[top - 1]), *play))
        g.executemany("INSERT INTO theme_score(node_id, anchor_id, score) VALUES (?,?,?)",
                      [(node_id, t.id, float(d[i])) for i, t in enumerate(THEMES)])
    m = {
        "schema_version": SCHEMA_VERSION, "backend": backend, "model": model,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "song_count": str(len(songs)), "lyrics_count": str(counts.get("lyrics", 0)),
        "title_count": str(counts.get("title", 0)), "ring_radius": repr(float(radius)),
        "angle_unit": "degrees", "anchor_plane": "xz", "midi_base": "relative to this file's folder",
    }
    m.update({k: str(v) for k, v in (meta or {}).items()})
    g.executemany("INSERT INTO themes_meta(key, value) VALUES (?,?)", sorted(m.items()))
    g.commit()
    g.close()
    os.replace(tmp, out)
    return {"path": str(out), "songs": len(songs), "by_source": counts, "top_theme_counts": top_counts,
            "singer_genders": gender_counts, "with_playback": sum(1 for s in songs if s[4] is not None)}
