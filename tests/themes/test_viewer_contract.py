"""The LyricThemes Unity viewer against the themes graph contract (DESIGN.md §12).

The viewer's C# reader (MusicHistory-Viewer: Assets/MusicHistory/Themes/ThemesGraphData.cs) keeps its SQL in
string constants. These tests run exactly those queries against the exporter's GRAPH_SCHEMA, so a
column renamed on either side fails here rather than in Unity; they also check that the viewer
names the ten themes in the exporter's order, selects no text column other than the fixed
metadata, and that its scene is registered in the build settings. The fixture rows are invented
(titles "Song A" etc.); no lyrics are involved anywhere.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory import config  # noqa: E402
from musichistory.themes import export  # noqa: E402
from musichistory.themes.classify import THEMES  # noqa: E402

UNITY = config.VIEWER  # sibling repository ../MusicHistory-Viewer (MUSICHISTORY_VIEWER overrides)
READER = UNITY / "Assets" / "MusicHistory" / "Themes" / "ThemesGraphData.cs"
THEMES_DIR = UNITY / "Assets" / "MusicHistory" / "Themes"
SCENE = UNITY / "Assets" / "Scenes" / "LyricThemes.unity"
BUILD_SETTINGS = UNITY / "ProjectSettings" / "EditorBuildSettings.asset"

pytestmark = pytest.mark.skipif(not READER.exists(), reason="Unity themes viewer not present")


def _cs_constant(source: str, name: str) -> str:
    """The value of `public const string <name> = "..." + "...";` (concatenated literals)."""
    m = re.search(rf"const string {name}\s*=\s*(.*?);", source, re.S)
    assert m, f"{name} not found in {READER.name}"
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))


def _queries() -> dict[str, str]:
    source = READER.read_text(encoding="utf-8")
    return {n: _cs_constant(source, n) for n in ("MetaQuery", "AnchorQuery", "SongQuery", "ScoreQuery", "LayoutRunQuery")}


def _graph(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "themes_graph.db")
    conn.executescript(export.GRAPH_SCHEMA)
    conn.executemany("INSERT INTO theme_anchor VALUES (?,?,?,?,?,?,?)", export.anchor_rows())
    songs = [(1, "R000000000001", "Song A", "Artist A", 1960, "male", "lyrics", 3, 0.7),
             (2, "R000000000002", "Song B", "Artist B", 1970, "female", "title", 10, 0.9)]
    for node, wid, title, artist, year, gender, source, top, score in songs:
        conn.execute("INSERT INTO theme_song(node_id, work_id, title, artist, year, singer_gender, text_source, top_anchor,"
                     " top_score, midi_path, excerpt_start_beat, excerpt_end_beat, tonic_pc, mode, native_bpm, beats_per_bar,"
                     " first_downbeat) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (node, wid, title, artist, year, gender, source, top, score, f"../songs/{wid}/score.mid",
                      0.0, 32.0, 0, "major", 120.0, 4.0, 0.0))
        rest = (1.0 - score) / (len(THEMES) - 1)
        conn.executemany("INSERT INTO theme_score VALUES (?,?,?)",
                         [(node, t.id, score if t.id == top else rest) for t in THEMES])
    conn.execute("INSERT INTO themes_meta VALUES ('song_count', '2')")
    conn.execute("INSERT INTO themes_layout_run(run_id, created_at, device, iterations, sharpen, repulsion, final_mean_move,"
                 " params_json) VALUES (1, '2026-09-30T00:00:00Z', 'cpu', 10, 2.0, 0.35, 0.001, '{}')")
    conn.commit()
    return conn


def test_viewer_queries_run_against_the_export_schema(tmp_path):
    conn = _graph(tmp_path)
    q = _queries()
    assert conn.execute(q["MetaQuery"]).fetchall() == [("song_count", "2")]
    anchors = conn.execute(q["AnchorQuery"]).fetchall()
    assert [a[0] for a in anchors] == list(range(1, 11))
    songs = conn.execute(q["SongQuery"]).fetchall()
    assert len(songs) == 2 and len(songs[0]) == 20
    assert [s[2] for s in songs] == ["Song A", "Song B"]
    assert len(conn.execute(q["ScoreQuery"]).fetchall()) == 20
    assert conn.execute(q["LayoutRunQuery"]).fetchone()[0] == 1


def test_viewer_song_columns_are_the_contract_columns(tmp_path):
    """The reader indexes columns by position: the SELECT list must stay in this order."""
    cols = re.search(r"SELECT (.*?) FROM theme_song", _queries()["SongQuery"]).group(1)
    names = [c.strip() for c in cols.split(",")]
    assert names == ["node_id", "work_id", "title", "artist", "year", "singer_gender", "text_source", "top_anchor",
                     "top_score", "position_x", "position_y", "position_z", "midi_path", "excerpt_start_beat",
                     "excerpt_end_beat", "tonic_pc", "mode", "native_bpm", "beats_per_bar", "first_downbeat"]
    schema = {r[1] for r in _graph(tmp_path).execute("PRAGMA table_info(theme_song)")}
    assert set(names) <= schema


def test_viewer_selects_no_free_text_beyond_titles_and_labels():
    """Only title/artist/labels/fixed enums are strings the viewer reads; nothing named like text or lyrics."""
    for name, sql in _queries().items():
        cols = re.search(r"SELECT (.*?) FROM", sql).group(1).lower()
        assert "lyric" not in cols, name
        for c in (c.strip() for c in cols.split(",")):
            assert c == "text_source" or "text" not in c, (name, c)


def test_viewer_names_the_ten_themes_in_export_order():
    source = READER.read_text(encoding="utf-8")
    block = source[source.index("DesignThemes"):]
    block = block[block.index("{") + 1:block.index("};")]
    labels = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    assert labels == [t.label for t in THEMES]
    assert [r[1] for r in export.anchor_rows()] == labels


def test_viewer_gender_vocabulary_matches_export():
    source = READER.read_text(encoding="utf-8")
    m = re.search(r"Genders\s*=\s*\{(.*?)\}", source, re.S)
    assert tuple(re.findall(r'"(\w+)"', m.group(1))) == export.SINGER_GENDERS


def test_lyric_themes_scene_is_in_the_build_settings():
    assert SCENE.exists()
    guid = re.search(r"guid: ([0-9a-f]{32})", (SCENE.parent / (SCENE.name + ".meta")).read_text()).group(1)
    settings = BUILD_SETTINGS.read_text()
    assert "path: Assets/Scenes/SongInfluenceGraph.unity" in settings
    entry = re.search(r"- enabled: 1\s+path: Assets/Scenes/LyricThemes\.unity\s+guid: ([0-9a-f]{32})", settings)
    assert entry and entry.group(1) == guid


def test_every_themes_asset_has_a_meta_file():
    files = [p for p in THEMES_DIR.rglob("*") if not p.name.endswith(".meta")]
    assert files
    for p in files + [THEMES_DIR, SCENE]:
        assert (p.parent / (p.name + ".meta")).exists(), p
