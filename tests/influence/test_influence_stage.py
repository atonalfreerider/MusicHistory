"""The influence stage (C#) end to end on a synthetic pipeline DB, checked from Python.

Builds ``influence/MusicHistory.Influence`` into a temporary folder, writes a fixture with
planted influence (``make-fixture``), runs ``run`` twice and ``export``, and checks:

* the graph DB schema equals DESIGN.md §10 (column names, types, NOT NULL, keys);
* invariants: node ids 1..N in (time_value, work_id) order, one tree edge into every non-root
  from its parent, sources earlier, CHECK(source < target), <= 8 secondary edges per target;
* MIDI paths relative to the graph DB folder with '/' separators;
* entry/exit keys (song_node.entry_*/exit_*) equal the fixture's key_region at the excerpt start
  and just before its end (NULL = home key), and graph_meta normalization/target_bpm come from the
  pipeline's analyze meta, not the exporting shell's environment;
* determinism (identical bytes with a fixed generated_at; export reproduces run);
* the file opens and answers queries in SQLite **3.15.0**, the version Unity ships
  (Unity-FDG's own sqlite3.dll, copied to a temp dir and loaded with ctypes);
* ``python -m musichistory influence`` (musichistory/dotnet.py) drives the same executable.
"""

from __future__ import annotations

import bisect
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSPROJ = ROOT / "influence" / "MusicHistory.Influence" / "MusicHistory.Influence.csproj"
UNITY_SQLITE = ROOT.parent / "Unity-FDG" / "Assets" / "Plugins" / "x86_64" / "sqlite3.dll"
STAMP = "2026-01-01T00:00:00Z"
N_SONGS = 120

pytestmark = pytest.mark.skipif(shutil.which("dotnet") is None, reason="needs the .NET 10 SDK")


@pytest.fixture(scope="module")
def exe(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("influence-bin")
    subprocess.run(["dotnet", "build", "-c", "Release", str(CSPROJ), "-o", str(out), "-nologo", "-v", "q"],
                   check=True, capture_output=True, text=True)
    return out / "MusicHistory.Influence.exe"


@pytest.fixture(scope="module")
def fixture_run(exe, tmp_path_factory):
    root = tmp_path_factory.mktemp("influence-fixture")
    data = root / "data"
    db = data / "musichistory.sqlite"
    subprocess.run([str(exe), "make-fixture", "--out", str(db), "--songs", str(N_SONGS), "--seed", "5"], check=True,
                   capture_output=True, text=True)
    graph = data / "graph" / "music_graph.db"
    r = subprocess.run([str(exe), "run", "--db", str(db), "--graph", str(graph), "--root", str(root),
                        "--generated-at", STAMP], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return {"root": root, "db": db, "graph": graph, "exe": exe, "log": r.stderr}


def _design_graph_schema() -> str:
    text = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    start = text.index("CREATE TABLE graph_meta")
    return text[start:text.index("```", start)]


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return [(r[1], r[2], r[3], r[5]) for r in conn.execute(f"PRAGMA table_info({table})")]


def test_graph_schema_matches_design(fixture_run):
    design = sqlite3.connect(":memory:")
    design.executescript(_design_graph_schema())
    g = sqlite3.connect(fixture_run["graph"])
    for table in ("graph_meta", "nodes", "song_node", "influence_edges"):
        assert _columns(g, table) == _columns(design, table), table
    sql = g.execute("SELECT sql FROM sqlite_master WHERE name = 'influence_edges'").fetchone()[0]
    assert "CHECK(source_node < target_node)" in sql
    assert g.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    meta = dict(g.execute("SELECT key, value FROM graph_meta"))
    for key in ("schema_version", "generated_at", "normalization", "target_key", "target_bpm", "min_time", "max_time",
                "song_count", "edge_count", "root_count", "resonance_commit", "pipeline_commit", "midi_base"):
        assert key in meta, key
    assert meta["schema_version"] == "1"
    assert meta["generated_at"] == STAMP
    assert meta["target_key"] == "C major / A minor"
    assert int(meta["song_count"]) == N_SONGS


def test_graph_invariants(fixture_run):
    g = sqlite3.connect(fixture_run["graph"])
    ids = [r[0] for r in g.execute("SELECT id FROM nodes ORDER BY id")]
    assert ids == list(range(1, N_SONGS + 1))
    assert g.execute("SELECT COUNT(*) FROM nodes WHERE position_x IS NOT NULL").fetchone()[0] == 0
    order = g.execute("SELECT node_id, time_value, work_id FROM song_node ORDER BY node_id").fetchall()
    assert order == sorted(order, key=lambda r: (r[1], r[2]))
    songs = {r[0]: r for r in g.execute("SELECT node_id, tree_parent_node, tree_root_node, tree_depth, time_value FROM song_node")}
    edges = g.execute("SELECT source_node, target_node, kind, similarity, weight FROM influence_edges").fetchall()
    tree_in = {}
    for s, t, kind, sim, w in edges:
        assert s < t and songs[s][4] < songs[t][4]
        assert 0 <= sim <= 1 and w > 0
        if kind == "tree":
            tree_in[t] = tree_in.get(t, 0) + 1
            assert songs[t][1] == s
    for nid, parent, root, depth, _ in songs.values():
        assert tree_in.get(nid, 0) == (1 if parent is not None else 0)
        if parent is None:
            assert root == nid and depth == 0
        else:
            assert depth == songs[parent][3] + 1 and root == songs[parent][2]
    per_target = {}
    for s, t, kind, *_ in edges:
        if kind == "secondary":
            per_target[t] = per_target.get(t, 0) + 1
    assert max(per_target.values(), default=0) <= 8
    assert len(edges) == len({(s, t) for s, t, *_ in edges})


def test_midi_paths_are_relative(fixture_run):
    g = sqlite3.connect(fixture_run["graph"])
    base = fixture_run["graph"].parent
    for wid, midi, norm, key in g.execute("SELECT work_id, midi_path, normalized_midi_path, key_name FROM song_node"):
        assert "\\" not in midi and not os.path.isabs(midi)
        assert midi == f"../songs/{wid}/score.mid"
        assert (base / midi).resolve() == (fixture_run["root"] / "data" / "songs" / wid / "score.mid").resolve()
        assert norm == f"../normalized/{wid}.mid"
        assert re.fullmatch(r"[A-G][b#]? (major|minor)", key)


def _region_at(regions: list[tuple], beat: float) -> tuple:
    """musichistory.identity.key.ShiftMap.region_at: last region starting at or before the beat, else the first."""
    starts = [r[0] for r in regions]
    return regions[max(0, bisect.bisect_right(starts, beat + 1e-6) - 1)]


def test_entry_exit_keys_follow_key_regions(fixture_run):
    """DESIGN.md §10 entry_*/exit_*: the key region at the excerpt start and just before its end, NULL = home key."""
    c = sqlite3.connect(fixture_run["db"])
    regions: dict[str, list[tuple]] = {}
    for wid, start, tonic, mode in c.execute("SELECT work_id, start_beat, tonic_pc, mode FROM key_region ORDER BY work_id, start_beat"):
        regions.setdefault(wid, []).append((start, tonic, mode))
    assert any(len(r) > 1 for r in regions.values()), "the fixture should contain modulating songs"
    g = sqlite3.connect(fixture_run["graph"])
    outside = 0
    for wid, tonic, mode, start, end, et, em, xt, xm in g.execute(
            "SELECT work_id, tonic_pc, mode, excerpt_start_beat, excerpt_end_beat, entry_tonic_pc, entry_mode, "
            "exit_tonic_pc, exit_mode FROM song_node"):
        def expect(beat: float) -> tuple:
            _, t, m = _region_at(regions[wid], beat)
            return (None, None) if (t, m) == (tonic, mode) else (t, m)
        assert (et, em) == expect(start), wid
        assert (xt, xm) == expect(end - 1e-3), wid
        outside += et is not None or xt is not None
    assert outside > 0


def test_graph_meta_comes_from_analyze_meta_not_environment(fixture_run, tmp_path):
    """Review finding influence #0: analyze records its settings in meta analyze_normalization /
    analyze_target_bpm; export must label the graph with them whatever the exporting shell's environment."""
    db = tmp_path / "data" / "musichistory.sqlite"
    db.parent.mkdir(parents=True)
    shutil.copyfile(fixture_run["db"], db)
    c = sqlite3.connect(db)
    c.execute("DELETE FROM meta WHERE key IN ('normalization', 'analyze_normalization', 'analyze_target_bpm')")
    c.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", [("analyze_normalization", "parallel"), ("analyze_target_bpm", "100")])
    if {"normalization", "target_bpm"} <= {r[1] for r in c.execute("PRAGMA table_info(song)")}:
        c.execute("UPDATE song SET normalization = 'parallel', target_bpm = 100")   # what analyze records per song
    c.commit()
    c.close()
    graph = tmp_path / "data" / "graph" / "music_graph.db"
    env = dict(os.environ, MUSICHISTORY_NORMALIZATION="relative", MUSICHISTORY_TARGET_BPM="120")
    r = subprocess.run([str(fixture_run["exe"]), "export", "--db", str(db), "--graph", str(graph), "--root", str(tmp_path),
                        "--generated-at", STAMP], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "WARNING" not in r.stderr
    meta = dict(sqlite3.connect(graph).execute("SELECT key, value FROM graph_meta"))
    assert (meta["normalization"], meta["target_key"], meta["target_bpm"]) == ("parallel", "C major / C minor", "100")


def test_summary_and_evidence_are_short_facts(fixture_run):
    g = sqlite3.connect(fixture_run["graph"])
    for (summary,) in g.execute("SELECT summary FROM song_node WHERE summary IS NOT NULL"):
        assert len(summary) <= 200 and summary.isascii()
    for (ev,) in g.execute("SELECT evidence FROM influence_edges"):
        assert ev and re.fullmatch(r"[a-z0-9 ,;()#b\-IViov?]+", ev), ev


def test_deterministic_and_export_reproduces(fixture_run, tmp_path):
    exe, db, root = fixture_run["exe"], fixture_run["db"], fixture_run["root"]
    first = hashlib.sha256(fixture_run["graph"].read_bytes()).hexdigest()
    # A second run (other thread count) and an export-only pass give the same bytes.
    # Same depth below the data folder, so the relative MIDI paths are the same strings.
    g2 = root / "data" / "graph2" / "music_graph.db"
    r = subprocess.run([str(exe), "run", "--db", str(db), "--graph", str(g2), "--root", str(root), "--generated-at", STAMP,
                        "--threads", "2", "--report", str(tmp_path / "r2.json")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert hashlib.sha256(g2.read_bytes()).hexdigest() == first
    g3 = root / "data" / "graph3" / "music_graph.db"
    r = subprocess.run([str(exe), "export", "--db", str(db), "--graph", str(g3), "--root", str(root), "--generated-at", STAMP],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert hashlib.sha256(g3.read_bytes()).hexdigest() == first


def test_pipeline_tables_and_report(fixture_run):
    c = sqlite3.connect(fixture_run["db"])
    assert c.execute("SELECT COUNT(*) FROM tree_node").fetchone()[0] == N_SONGS
    g = sqlite3.connect(fixture_run["graph"])
    assert c.execute("SELECT COUNT(*) FROM influence_edge").fetchone()[0] == g.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0]
    assert c.execute("SELECT COUNT(*) FROM pair_score WHERE significant = 1 AND relation <> 'influence'").fetchone()[0] == 0
    for (seg,) in c.execute("SELECT segments_json FROM pair_score WHERE segments_json IS NOT NULL LIMIT 50"):
        for s in json.loads(seg):
            assert {"channel", "a_start", "a_end", "b_start", "b_end", "bits"} <= set(s)
    rep = json.loads((fixture_run["graph"].parent / "influence_report.json").read_text(encoding="utf-8"))
    assert rep["graph"]["nodes"] == N_SONGS
    assert set(rep["timings_s"]) >= {"load", "score", "tree", "export", "total"}
    v = rep["validation"]
    assert v["summary"]["positive"]["present"] > 0
    neg = v["summary"]["negative"]
    assert neg.get("with_edge", 0) <= 0.02 * neg["present"]      # acceptance: <= 2 % of commonplace pairs
    assert v["summary"]["version"].get("classified_version", 0) == v["summary"]["version"]["present"]


def test_score_pairs_prints_json(fixture_run):
    c = sqlite3.connect(fixture_run["db"])
    src, dst = c.execute("SELECT src_work_id, dst_work_id FROM known_influence WHERE kind = 'control_positive' LIMIT 1").fetchone()
    r = subprocess.run([str(fixture_run["exe"]), "score-pairs", "--db", str(fixture_run["db"]), "--pairs", json.dumps([[src, dst]])],
                       capture_output=True, text=True, check=True)
    out = json.loads(r.stdout)
    assert out[0]["a_id"] == src and out[0]["b_id"] == dst
    assert set(out[0]["channels"]) == {"melody", "bass", "chord", "loop"}


@pytest.mark.skipif(sys.platform != "win32" or not UNITY_SQLITE.exists(), reason="needs Unity-FDG's sqlite3.dll (Windows)")
def test_opens_in_sqlite_3_15(fixture_run, tmp_path):
    dll = tmp_path / "sqlite3_315.dll"
    shutil.copyfile(UNITY_SQLITE, dll)          # never load it from the Unity project itself
    lib = ctypes.CDLL(str(dll))
    lib.sqlite3_libversion.restype = ctypes.c_char_p
    assert lib.sqlite3_libversion() == b"3.15.0"
    db = ctypes.c_void_p()
    rc = lib.sqlite3_open_v2(str(fixture_run["graph"]).encode("utf-8"), ctypes.byref(db), 1, None)  # SQLITE_OPEN_READONLY
    assert rc == 0
    lib.sqlite3_errmsg.restype = ctypes.c_char_p
    lib.sqlite3_column_text.restype = ctypes.c_char_p

    def query(sql: str) -> list[tuple]:
        stmt = ctypes.c_void_p()
        assert lib.sqlite3_prepare_v2(db, sql.encode(), -1, ctypes.byref(stmt), None) == 0, lib.sqlite3_errmsg(db)
        rows = []
        while lib.sqlite3_step(stmt) == 100:  # SQLITE_ROW
            n = lib.sqlite3_column_count(stmt)
            rows.append(tuple((lib.sqlite3_column_text(stmt, i) or b"").decode() for i in range(n)))
        lib.sqlite3_finalize(stmt)
        return rows

    try:
        assert query("PRAGMA integrity_check") == [("ok",)]
        assert query("SELECT COUNT(*) FROM song_node") == [(str(N_SONGS),)]
        # The loader's shape of query: nodes + songs + edges.
        rows = query("SELECT n.id, s.title, s.year, s.key_name, s.midi_path, s.excerpt_start_beat, s.tree_parent_node, "
                     "s.entry_tonic_pc, s.entry_mode, s.exit_tonic_pc, s.exit_mode "
                     "FROM nodes n JOIN song_node s ON s.node_id = n.id ORDER BY n.id")
        assert len(rows) == N_SONGS
        e315 = query("SELECT source_node, target_node, kind, primary_channel FROM influence_edges ORDER BY id")
        py = sqlite3.connect(fixture_run["graph"]).execute(
            "SELECT source_node, target_node, kind, primary_channel FROM influence_edges ORDER BY id").fetchall()
        assert e315 == [tuple(str(x) for x in r) for r in py]
        assert query("SELECT value FROM graph_meta WHERE key = 'schema_version'") == [("1",)]
    finally:
        lib.sqlite3_close(db)


def test_cli_drives_the_executable(tmp_path):
    """``python -m musichistory influence`` builds into DATA/tools and runs against DATA's DBs."""
    data = tmp_path / "data"
    env = dict(os.environ, MUSICHISTORY_DATA=str(data), MUSICHISTORY_CACHE=str(ROOT / "data" / "cache"))
    exe_dir = tmp_path / "bin"
    subprocess.run(["dotnet", "build", "-c", "Release", str(CSPROJ), "-o", str(exe_dir), "-nologo", "-v", "q"],
                   check=True, capture_output=True, text=True)
    subprocess.run([str(exe_dir / "MusicHistory.Influence.exe"), "make-fixture", "--out", str(data / "musichistory.sqlite"),
                    "--songs", "40", "--seed", "9"], check=True, capture_output=True, text=True)
    r = subprocess.run([sys.executable, "-m", "musichistory", "influence", "--", "--generated-at", STAMP],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    g = sqlite3.connect(data / "graph" / "music_graph.db")
    assert g.execute("SELECT COUNT(*) FROM song_node").fetchone()[0] == 40
    assert (data / "graph" / "influence_report.json").exists()
