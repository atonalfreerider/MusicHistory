"""The influence stage (C#) end to end on a synthetic pipeline DB, checked from Python.

Builds ``influence/MusicHistory.Influence`` into a temporary folder, writes a fixture with
planted influence (``make-fixture``), runs ``run`` twice and ``export``, and checks, for the strict
evidence graph (``run --mode evidence``, fixture ``fixture_run``):

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

and for the identity lineages (DESIGN.md §8b, ``run`` without ``--mode``, the default; fixture ``lineage_run``):

* graph_meta.edge_semantics = 'identity_lineage', the extra tables identity_family / song_family as §8b
  declares them, every edge's evidence the label of a family both songs belong to, z only on strong
  matches, excerpts of 8..24 bars, the §10 invariants;
* determinism, and ``export`` / ``retree`` reproduce the run; the report's families and tree sections;
* the evidence graph carries none of it (no identity tables, no edge_semantics: byte-identical to before).
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
# The fixture's 120 songs make every time-ordered pair part of the null sample, planted pairs included, so its
# threshold at the real corpus's calibrated FPR (3e-5 of 507,740 pairs) is dominated by the plants: run it at 1e-3.
FIXTURE_FPR = ["--param", "TargetFpr=1e-3"]

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
    r = subprocess.run([str(exe), "run", "--mode", "evidence", "--db", str(db), "--graph", str(graph), "--root", str(root),
                        "--generated-at", STAMP, *FIXTURE_FPR], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return {"root": root, "db": db, "graph": graph, "exe": exe, "log": r.stderr}


@pytest.fixture(scope="module")
def lineage_run(exe, tmp_path_factory):
    """The default mode (identity lineages) on its own copy of the same fixture."""
    root = tmp_path_factory.mktemp("influence-lineage")
    data = root / "data"
    db = data / "musichistory.sqlite"
    subprocess.run([str(exe), "make-fixture", "--out", str(db), "--songs", str(N_SONGS), "--seed", "5"], check=True,
                   capture_output=True, text=True)
    graph = data / "graph" / "music_graph.db"
    r = subprocess.run([str(exe), "run", "--db", str(db), "--graph", str(graph), "--root", str(root),
                        "--generated-at", STAMP, *FIXTURE_FPR], capture_output=True, text=True)
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
    r = subprocess.run([str(exe), "run", "--mode", "evidence", "--db", str(db), "--graph", str(g2), "--root", str(root), "--generated-at", STAMP,
                        "--threads", "2", "--report", str(tmp_path / "r2.json"), *FIXTURE_FPR], capture_output=True, text=True)
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
    # V2: every channel of the V8 evidence is listed (lanes is null without lane:* rows); the pair's z is the
    # best window's fused z and is compared with the run's recorded threshold.
    assert set(out[0]["channels"]) == {"melody", "bass", "chord", "loop", "lanes"}
    assert out[0]["channels"]["lanes"] is None
    assert out[0]["threshold_z"] is not None and isinstance(out[0]["above_threshold"], bool)
    assert out[0]["best_windows"] and out[0]["z"] == pytest.approx(max(w["fused_z"] for w in out[0]["best_windows"]), abs=1e-3)
    assert 0 < out[0]["windows_above_half"] <= out[0]["windows"]


def test_score_pairs_debug_places_the_shared_material_in_both_songs(fixture_run):
    """--debug lists every shared rare n-gram of the winning window with its span in the later song and its
    occurrences in the earlier one (for the side-by-side audit), and ranks the earlier song among all earlier songs
    on that window."""
    c = sqlite3.connect(fixture_run["db"])
    src, dst = c.execute("SELECT a_id, b_id FROM pair_score WHERE significant = 1 ORDER BY z_combined DESC LIMIT 1").fetchone()
    r = subprocess.run([str(fixture_run["exe"]), "score-pairs", "--db", str(fixture_run["db"]), "--pairs", json.dumps([[src, dst]]),
                        "--debug"], capture_output=True, text=True, check=True)
    out = json.loads(r.stdout)[0]
    grams = [g for g in out["shared_rare_ngrams"] if g["bits"] > 0]
    assert grams
    w0, w1 = out["window_beats"]
    for g in grams:
        assert {"channel", "family", "df", "pair_df", "bits", "b_beat", "b_end_beat", "b_line", "a_line", "a_beats"} <= set(g)
        assert w0 - 1e-6 <= g["b_beat"] <= g["b_end_beat"] < w1 + 1e-6
        assert g["a_beats"] and all(s <= e for s, e in g["a_beats"]) and len(g["a_beats"]) <= 4
        assert g["channel"] in ("melody", "bass", "chord", "lanes")
    assert out["window_z"] == pytest.approx(out["z"], abs=1e-3)
    assert out["window_rank"] >= 1 and out["window_others_half"] >= 0


def test_decision_is_recorded_in_graph_meta_and_report(fixture_run):
    """The empirical threshold of the run is written to graph_meta (influence_*) and the report, and every edge clears it."""
    g = sqlite3.connect(fixture_run["graph"])
    meta = dict(g.execute("SELECT key, value FROM graph_meta WHERE key LIKE 'influence%'"))
    t = float(meta["influence_threshold_z"])
    rep = json.loads((fixture_run["graph"].parent / "influence_report.json").read_text(encoding="utf-8"))
    fpr = rep["params"]["TargetFpr"]
    assert fpr == 1e-3                                               # FIXTURE_FPR (the default, 3e-5, is checked in CalibrationTests)
    assert float(meta["influence_target_fpr"]) == fpr
    assert int(meta["influence_null_sample"]) > 0 and meta["influence_threshold_method"]
    # The evidence settings of the V2.1 calibration are recorded next to the decision.
    assert meta["influence_df_cap"] == str(rep["params"]["DfCap"])
    assert meta["influence_lanes"] == "off" and meta["influence_key_free_df"].startswith("2 ")
    assert "pitch classes" in meta["influence_line_filter"]
    assert g.execute("SELECT COUNT(*) FROM influence_edges WHERE z <= ?", (t,)).fetchone()[0] == 0
    d = rep["decision"]
    assert d["threshold_z"] == pytest.approx(t, abs=1e-6)
    assert d["expected_false_pairs"] == pytest.approx(fpr * d["time_ordered_pairs"], rel=1e-9)
    # Diagnostics: the operating curve (expected chance pairs per FPR) and the top of the null sample.
    curve = d["operating_curve"]
    assert [x["fpr"] for x in curve] == [1e-2, 3e-3, 1e-3, 3e-4, 1e-4, 3e-5, 1e-5]
    for x in curve:
        assert x["expected_false"] == pytest.approx(x["fpr"] * d["time_ordered_pairs"], rel=1e-9)
    top = d["null_sample_top"]
    assert top and all(a["z"] >= b["z"] for a, b in zip(top, top[1:]))
    # The pipeline meta holds the same keys, so export alone reproduces them.
    c = sqlite3.connect(fixture_run["db"])
    assert dict(c.execute("SELECT key, value FROM meta WHERE key LIKE 'influence%'")) == meta


BENCHMARK = os.environ.get("MUSICHISTORY_BENCHMARK")


@pytest.mark.skipif(not BENCHMARK or not (Path(BENCHMARK or ".") / "pairs.json").exists(),
                    reason="set MUSICHISTORY_BENCHMARK to the calibration benchmark folder (diag/benchmark)")
def test_benchmark_port_reproduces_python_v8(exe):
    """evaluate --benchmark: the C# V8 reproduces the Python analytic.py z of every benchmark pair (df cap 5 and
    uncapped, per channel and fused) and therefore its TPR at a window FPR of 1e-3."""
    r = subprocess.run([str(exe), "evaluate", "--benchmark", BENCHMARK], capture_output=True, text=True, timeout=1800)
    assert r.returncode == 0, r.stderr
    rows = [line for line in r.stdout.splitlines() if line.startswith("| 5 |") or line.startswith("| none |")]
    assert len(rows) >= 10
    for line in rows:
        cells = [c.strip() for c in line.strip("|").split("|")]
        cs_tpr, py_tpr, bad = cells[2].split()[0], cells[3].split()[0], cells[9]
        if py_tpr == "-":
            continue
        assert cs_tpr == py_tpr, line
        assert bad == "0", line


@pytest.mark.skipif(sys.platform != "win32" or not UNITY_SQLITE.exists(), reason="needs Unity-FDG's sqlite3.dll (Windows)")
@pytest.mark.parametrize("which", ["fixture_run", "lineage_run"])
def test_opens_in_sqlite_3_15(which, request, tmp_path):
    fixture_run = request.getfixturevalue(which)
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
        if which == "lineage_run":
            # The identity tables and the viewer's family lookup (an edge's identity = a family of both songs with its label).
            fam = query("SELECT e.id, f.family_id FROM influence_edges e JOIN identity_family f ON f.label = e.evidence "
                        "JOIN song_family a ON a.family_id = f.family_id AND a.node_id = e.source_node "
                        "JOIN song_family b ON b.family_id = f.family_id AND b.node_id = e.target_node ORDER BY e.id")
            assert len({r[0] for r in fam}) == len(e315)
            assert query("SELECT value FROM graph_meta WHERE key = 'edge_semantics'") == [("identity_lineage",)]
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
    # The default mode is the identity lineages (DESIGN.md §8b).
    assert g.execute("SELECT value FROM graph_meta WHERE key = 'edge_semantics'").fetchone() == ("identity_lineage",)


# ---------------------------------------------------------------------------------------------- identity lineages
def _design_lineage_tables() -> str:
    """The §8b DDL of the extra graph tables, as DESIGN.md states it inline."""
    text = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    sec = text[text.index("**Extra graph tables**"):]
    out = []
    for name in ("identity_family", "song_family"):
        m = re.search(r"`(" + name + r"\(.*?\))`", sec, re.S)
        assert m, name
        out.append("CREATE TABLE " + " ".join(m.group(1).split()) + ";")
    return "\n".join(out)


@pytest.mark.parametrize("which", ["fixture_run", "lineage_run"])
def test_graph_meets_the_unity_loader_contract(which, request):
    """The viewer side's own conformance check (tests/viewer/graph_fixture.py) passes on both graphs."""
    path = ROOT / "tests" / "viewer" / "graph_fixture.py"
    if not path.exists():
        pytest.skip("tests/viewer/graph_fixture.py not present")
    import importlib.util
    spec = importlib.util.spec_from_file_location("mh_unity_graph_fixture", path)
    gf = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gf              # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(gf)
    assert gf.check_graph(request.getfixturevalue(which)["graph"]) == []


def test_lineage_is_the_default_and_marks_the_graph(lineage_run):
    g = sqlite3.connect(lineage_run["graph"])
    meta = dict(g.execute("SELECT key, value FROM graph_meta"))
    assert meta["edge_semantics"] == "identity_lineage"
    assert int(meta["family_count"]) == g.execute("SELECT COUNT(*) FROM identity_family").fetchone()[0] > 0
    c = sqlite3.connect(lineage_run["db"])
    assert dict(c.execute("SELECT key, value FROM meta WHERE key LIKE 'influence%'")) == {
        k: v for k, v in meta.items() if k.startswith("influence")}
    assert meta["influence_edge_semantics"] == "identity_lineage"


def test_evidence_graph_has_no_identity_tables(fixture_run):
    """--mode evidence is the strict graph exactly as before: no identity tables, no edge_semantics key."""
    g = sqlite3.connect(fixture_run["graph"])
    names = {r[0] for r in g.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == {"graph_meta", "nodes", "song_node", "influence_edges"}
    meta = dict(g.execute("SELECT key, value FROM graph_meta"))
    assert "edge_semantics" not in meta and "influence_edge_semantics" not in meta


def test_lineage_graph_schema_matches_design(lineage_run):
    design = sqlite3.connect(":memory:")
    design.executescript(_design_graph_schema())
    design.executescript(_design_lineage_tables())
    g = sqlite3.connect(lineage_run["graph"])
    for table in ("graph_meta", "nodes", "song_node", "influence_edges", "identity_family", "song_family"):
        assert _columns(g, table) == _columns(design, table), table
    assert g.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_lineage_edges_name_a_shared_family(lineage_run):
    g = sqlite3.connect(lineage_run["graph"])
    fam = {r[0]: r for r in g.execute("SELECT family_id, label, kind, roman, size FROM identity_family")}
    members: dict[int, dict[int, tuple]] = {}
    for node, fid, strength, first in g.execute("SELECT node_id, family_id, strength, first_beat FROM song_family"):
        members.setdefault(fid, {})[node] = (strength, first)
        assert 0 < strength <= 1
    for fid, (_, label, kind, roman, size) in fam.items():
        assert kind in ("schema", "loop", "progression", "strong")
        assert size == len(members.get(fid, {}))
        assert label.isascii() and 0 < len(label) <= 80
    labels = [f[1] for f in fam.values() if f[2] != "strong"]
    assert len(labels) == len(set(labels))                      # unique outside strong matches
    songs = {r[0]: r for r in g.execute("SELECT node_id, time_value, beats_per_bar, tree_parent_node FROM song_node")}
    edges = g.execute("SELECT source_node, target_node, kind, evidence, z, primary_channel, src_start_beat, src_end_beat, "
                      "dst_start_beat, dst_end_beat, channels FROM influence_edges").fetchall()
    assert edges
    for s, t, kind, ev, z, primary, s0, s1, d0, d1, channels in edges:
        assert s < t and songs[s][1] < songs[t][1]
        shared = [f for f in fam.values() if f[1] == ev and s in members.get(f[0], {}) and t in members.get(f[0], {})]
        assert len(shared) == 1, (s, t, ev)
        strong = shared[0][2] == "strong"
        assert (z != 0) == strong
        assert primary in ("melody", "bass", "chord", "loop")
        assert primary in channels.split(","), (s, t, channels, primary)   # the viewer's contract
        if shared[0][2] in ("schema", "loop"):
            assert primary == "loop"
        for a, b, node in ((s0, s1, s), (d0, d1, t)):
            bpb = songs[node][2]
            assert 8 * bpb - 1e-6 <= b - a <= 24 * bpb + 1e-6
        if kind == "tree":
            assert songs[t][3] == s


def test_lineage_deterministic_export_and_retree_reproduce(lineage_run, tmp_path):
    exe, db, root = lineage_run["exe"], lineage_run["db"], lineage_run["root"]
    first = hashlib.sha256(lineage_run["graph"].read_bytes()).hexdigest()
    g2 = root / "data" / "graph2" / "music_graph.db"
    r = subprocess.run([str(exe), "run", "--db", str(db), "--graph", str(g2), "--root", str(root), "--generated-at", STAMP,
                        "--threads", "2", "--report", str(tmp_path / "r2.json"), *FIXTURE_FPR], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert hashlib.sha256(g2.read_bytes()).hexdigest() == first
    for cmd, name in (("export", "graph3"), ("retree", "graph4")):
        gx = root / "data" / name / "music_graph.db"
        r = subprocess.run([str(exe), cmd, "--db", str(db), "--graph", str(gx), "--root", str(root), "--generated-at", STAMP],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert hashlib.sha256(gx.read_bytes()).hexdigest() == first, cmd


def test_lineage_report_sections(lineage_run):
    rep = json.loads((lineage_run["graph"].parent / "influence_report.json").read_text(encoding="utf-8"))
    assert rep["edge_semantics"] == "identity_lineage"
    g = sqlite3.connect(lineage_run["graph"])
    fam = rep["families"]
    assert fam["total"] == g.execute("SELECT COUNT(*) FROM identity_family").fetchone()[0]
    assert sum(fam["by_kind"].values()) == fam["total"]
    assert fam["singletons"] == g.execute("SELECT COUNT(*) FROM identity_family WHERE size = 1").fetchone()[0]
    assert sum(fam["size_histogram"].values()) == fam["total"]
    for t in fam["top"]:
        assert {"label", "kind", "size", "root_song", "tree_edges", "tree_shape"} <= set(t)
    tree = rep["tree"]
    n_tree = g.execute("SELECT COUNT(*) FROM influence_edges WHERE kind = 'tree'").fetchone()[0]
    assert tree["tree_edges"] == n_tree == N_SONGS - tree["roots"]
    assert sum(tree["depth_histogram"].values()) == N_SONGS
    assert tree["max_chain_songs"] == tree["max_depth"] + 1
    assert tree["largest_subtrees"] and tree["longest_chains"]
    assert len(tree["longest_chains"][0]) == tree["max_depth"] + 1
    assert [v["rule"].split(":")[0] for v in tree["guard"]][-1] == "final"
    assert tree["hubs"]["max_children"] == max(r[0] for r in g.execute(
        "SELECT COUNT(*) FROM influence_edges WHERE kind = 'tree' GROUP BY source_node"))
    assert rep["strict_evidence_graph"]["nodes"] == N_SONGS
    assert "decision" in rep and rep["lineage_params"]["StrongWeight"] > 0
