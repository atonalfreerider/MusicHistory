"""Tests for the Unity viewer's graph fixture and the §10 contract checker."""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import mido
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import graph_fixture as gf  # noqa: E402

TEXT_META = {"lyrics", "text", "marker", "cue_marker", "copyright", "instrument_name",
             "sequencer_specific", "key_signature"}

needs_sources = pytest.mark.skipif(not gf.DEFAULT_SOURCE.exists(), reason="prototype test MIDIs not present")


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not gf.DEFAULT_SOURCE.exists():
        pytest.skip("prototype test MIDIs not present")
    return gf.build_fixture(tmp_path_factory.mktemp("unity_fixture"), gf.DEFAULT_SOURCE)


@needs_sources
def test_fixture_conforms_to_contract(fixture_db: Path) -> None:
    assert gf.check_graph(fixture_db) == []
    conn = sqlite3.connect(fixture_db)
    n, roots, edges, tree = conn.execute(
        "SELECT (SELECT COUNT(*) FROM song_node), (SELECT COUNT(*) FROM song_node WHERE tree_parent_node IS NULL),"
        " (SELECT COUNT(*) FROM influence_edges), (SELECT COUNT(*) FROM influence_edges WHERE kind='tree')").fetchone()
    positions = conn.execute("SELECT COUNT(*) FROM nodes WHERE position_x IS NOT NULL").fetchone()[0]
    conn.close()
    assert n == len(gf.FIXTURE_SONGS)
    assert tree == n - roots
    assert edges == tree + len(gf.FIXTURE_SECONDARY)
    assert positions == 0, "fixture leaves positions NULL so the loader's fallback layout is tested"


@needs_sources
def test_fixture_midi_paths_resolve_and_carry_no_text(fixture_db: Path) -> None:
    conn = sqlite3.connect(fixture_db)
    paths = [r[0] for r in conn.execute("SELECT midi_path FROM song_node ORDER BY node_id")]
    conn.close()
    for rel in paths:
        path = (fixture_db.parent / rel).resolve()
        assert path.is_file(), rel
        mid = mido.MidiFile(path, clip=True)
        assert mid.type == 1
        # Only message types are inspected; payloads are never read or printed.
        kinds = {msg.type for track in mid.tracks for msg in track if msg.is_meta}
        assert not (kinds & TEXT_META), f"{rel} still has {sorted(kinds & TEXT_META)}"
        assert sum(1 for t in mid.tracks for m in t if m.type == "note_on" and m.velocity > 0) > 0


@needs_sources
def test_checker_catches_contract_violations(fixture_db: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.db"
    shutil.copy(fixture_db, bad)
    conn = sqlite3.connect(bad)
    conn.execute("DELETE FROM influence_edges WHERE kind='tree' AND target_node=(SELECT MAX(target_node)"
                 " FROM influence_edges WHERE kind='tree')")
    conn.execute("UPDATE song_node SET descendants = descendants + 1 WHERE node_id = 1")
    conn.execute("UPDATE song_node SET midi_path = 'C:\\x\\score.mid' WHERE node_id = 2")
    conn.commit()
    conn.close()
    problems = gf.check_graph(bad)
    assert any("expected exactly one" in p for p in problems)
    assert any("descendants" in p for p in problems)
    assert any("midi_path" in p for p in problems)
    assert any("in/out degree" in p for p in problems)

    wal = tmp_path / "wal.db"
    shutil.copy(fixture_db, wal)
    conn = sqlite3.connect(wal)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    assert any("journal_mode" in p for p in gf.check_graph(wal))


def test_key_names_follow_design_spelling() -> None:
    assert gf.key_name(0, False) == "C major"
    assert gf.key_name(10, False) == "Bb major"
    assert gf.key_name(6, False) == "Gb major"
    assert gf.key_name(6, True) == "F# minor"
    assert gf.key_name(3, True) == "Eb minor"
    assert gf.key_name(1, True) == "C# minor"
    assert gf.key_name(9, True) == "A minor"


@pytest.mark.parametrize("name", ["demo_graph.db", "music_graph.db"])
def test_pipeline_graph_dbs_conform(name: str) -> None:
    path = gf.ROOT / "data" / "graph" / name
    if not path.exists():
        pytest.skip(f"{name} not produced yet")
    assert gf.check_graph(path) == []
