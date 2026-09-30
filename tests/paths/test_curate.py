"""Curation rules on a small synthetic graph, and the committed curated.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory import config  # noqa: E402
from musichistory.paths import curate  # noqa: E402
from musichistory.paths.graph import Edge, Family, Graph, Song  # noqa: E402
from musichistory.paths.measured import Measured  # noqa: E402

FAM = {
    1: Family(1, "axis progression I-V-vi-IV", "schema", "I-V-vi-IV", 40),
    2: Family(2, "two-chord vamp I-IV", "schema", "I-IV", 42),
    3: Family(3, "exact melody passage, 19 notes", "strong", None, 2),
    4: Family(4, "ii-V-I cadence", "progression", "ii-V-I", 157),
    5: Family(5, "I-V-IV-V", "loop", "I-V-IV-V", 8),
    6: Family(6, "Pachelbel ground I-V-vi-III-IV-I-IV-V", "schema", "vi-III-IV-I-IV-V-I-V", 3),
}


def song(nid: int, year: int, rank: int | None = 100, preview: bool = True, artist_sim: float = 1.0) -> Song:
    s = Song(nid, f"Q{nid}", f"Song {nid}", f"Artist {nid}", year, year + 0.5, rank, 0, "major", "C major", 120.0, 4.0)
    s.preview = Path(f"/audio/Q{nid}/preview.mp3") if preview else None
    s.preview_artist = artist_sim
    return s


def meas(nid: int, tonic: int = 0, bpm: float = 120.0) -> Measured:
    return Measured(nid, f"Q{nid}", tonic, "major", "audio", 1.0, 0.3, False, bpm, "audio", 1.0, bpm, 0.8,
                    tonic, "major", bpm, 30.0)


def edge(a: int, b: int, fam: int, kind: str = "tree", z: float = 0.0) -> Edge:
    return Edge(a, b, kind, FAM[fam].label, z, 5.0, FAM[fam])


def build(edges: list[Edge], songs: list[Song], measured: dict[int, Measured] | None = None):
    g = Graph({s.node_id: s for s in songs}, {(e.source, e.target): e for e in edges}, dict(FAM))
    m = measured if measured is not None else {s.node_id: meas(s.node_id) for s in songs}
    return g, m


def test_identity_weights_order():
    strong = curate.identity_weight(edge(1, 2, 3, z=40))[0]
    pach = curate.identity_weight(edge(1, 2, 6))[0]
    axis = curate.identity_weight(edge(1, 2, 1))[0]
    loop4 = curate.identity_weight(edge(1, 2, 5))[0]
    vamp = curate.identity_weight(edge(1, 2, 2))[0]
    cadence = curate.identity_weight(edge(1, 2, 4))[0]
    assert strong > axis and pach > axis > loop4 > vamp
    assert vamp <= curate.GENERIC_MAX and cadence <= curate.GENERIC_MAX
    assert curate.identity_weight(edge(1, 2, 3, z=40))[1] == "exact melody"


def test_paths_follow_edges_forward_in_time_with_trusted_previews():
    songs = [song(1, 1960), song(2, 1970), song(3, 1980), song(4, 1990), song(5, 1995, preview=False),
             song(6, 2000, artist_sim=0.2)]
    edges = [edge(1, 2, 1), edge(2, 3, 1), edge(3, 4, 1), edge(4, 5, 1), edge(3, 6, 1)]
    g, m = build(edges, songs)
    cands = curate.enumerate_paths(g, m)
    got = {tuple(c.nodes) for c in cands}
    assert got == {(1, 2, 3), (2, 3, 4), (1, 2, 3, 4)}           # no 5 (no preview), no 6 (wrong artist)
    for c in cands:
        for a, b in zip(c.nodes, c.nodes[1:]):
            assert (a, b) in g.edges and g.songs[a].time_value < g.songs[b].time_value


def test_length_limits():
    songs = [song(i, 1950 + i) for i in range(1, 10)]
    edges = [edge(i, i + 1, 1) for i in range(1, 9)]
    g, m = build(edges, songs)
    lengths = {len(c.nodes) for c in curate.enumerate_paths(g, m)}
    assert lengths == {3, 4, 5, 6}


def test_at_most_one_generic_hop():
    songs = [song(i, 1950 + 5 * i) for i in range(1, 6)]
    edges = [edge(1, 2, 2), edge(2, 3, 1), edge(3, 4, 2), edge(4, 5, 1)]
    g, m = build(edges, songs)
    for c in curate.enumerate_paths(g, m):
        assert sum(1 for h in c.hops if h.generic) <= 1
    assert (1, 2, 3, 4) not in {tuple(c.nodes) for c in curate.enumerate_paths(g, m)}


def test_smoothness_uses_measured_keys_and_tempos():
    songs = [song(1, 1960), song(2, 1970), song(3, 1980)]
    edges = [edge(1, 2, 1), edge(2, 3, 1)]
    g, m = build(edges, songs, {1: meas(1, 0, 100), 2: meas(2, 5, 100), 3: meas(3, 5, 135)})
    h12 = curate.make_hop(g, m, g.edge(1, 2))
    h23 = curate.make_hop(g, m, g.edge(2, 3))
    assert h12.semitones == -5 and not h12.smooth                # F heard from C: 5 down
    assert h23.semitones == 0 and h23.ratio == pytest.approx(100 / 135) and not h23.smooth
    assert h12.smooth_cost() > 0.4 and h23.smooth_cost() > 0.12
    ok = curate.make_hop(*build(edges, songs, {1: meas(1, 0, 100), 2: meas(2, 2, 110), 3: meas(3, 2, 110)}),
                         edge(1, 2, 1))
    assert ok.smooth and ok.smooth_cost() < 0.05


def test_score_prefers_strong_coherent_smooth_paths():
    songs = [song(i, 1950 + 10 * i, rank=50) for i in range(1, 8)]
    edges = [edge(1, 2, 1), edge(2, 3, 1), edge(4, 5, 2), edge(5, 6, 4)]
    g, m = build(edges, songs)
    cands = {tuple(c.nodes): c for c in curate.enumerate_paths(g, m, max_generic=2)}
    assert cands[(1, 2, 3)].score > cands[(4, 5, 6)].score
    assert cands[(4, 5, 6)].terms["generic_hops"] == 2


def test_select_is_diverse():
    songs = [song(i, 1950 + 5 * i) for i in range(1, 9)]
    edges = [edge(1, 2, 1), edge(2, 3, 1), edge(3, 4, 1), edge(5, 6, 1), edge(6, 7, 1), edge(7, 8, 5)]
    g, m = build(edges, songs)
    chosen = curate.select(curate.enumerate_paths(g, m), 5)
    seen = set()
    for c in chosen:
        assert not (set(c.nodes) & seen)
        seen |= set(c.nodes)
    assert len(chosen) == 2


def test_validate_curated_rules():
    songs = [song(1, 1960), song(2, 1970), song(3, 1980), song(4, 1990, preview=False), song(5, 1995, artist_sim=0.1)]
    edges = [edge(1, 2, 1), edge(2, 3, 1), edge(3, 4, 1), edge(3, 5, 1)]
    g, m = build(edges, songs)
    ok = curate.CuratedPath("axis-story", "Axis", "The axis progression carries through.", ["Q1", "Q2", "Q3"])
    assert curate.validate_curated(g, m, ok) == []

    def errs(**kw):
        cp = curate.CuratedPath(**{**ok.__dict__, **kw})
        return " | ".join(curate.validate_curated(g, m, cp))
    assert "no edge" in errs(works=["Q1", "Q3", "Q2"])
    assert "no preview" in errs(works=["Q2", "Q3", "Q4"])
    assert "another artist" in errs(works=["Q2", "Q3", "Q5"])
    assert "songs" in errs(works=["Q1", "Q2"])
    assert "slug" in errs(id="Axis Story")
    assert "music only" in errs(description="The lyrics say so.")
    assert "not in the graph" in errs(works=["Q1", "Q2", "Q99"])


def test_subtitle_format():
    songs = [song(1, 1997), song(2, 2005), song(3, 2019)]
    g, m = build([edge(1, 2, 6), edge(2, 3, 6)], songs)
    c = curate.curated_candidate(g, m, curate.CuratedPath("p", "t", "d", ["Q1", "Q2", "Q3"]))
    assert curate.subtitle(g, c) == "1997 -> 2019 · 3 songs · chord progression"


# ------------------------------------------------------------------ committed file
def test_committed_curated_json_is_well_formed():
    doc = json.loads(curate.CURATED_PATH.read_text(encoding="utf-8"))
    paths = doc["paths"]
    assert 6 <= len(paths) <= 12   # quality gate first: fewer, nicer paths are fine
    ids = [p["id"] for p in paths]
    assert len(set(ids)) == len(ids)
    for p in paths:
        assert curate.SLUG.match(p["id"])
        assert curate.MIN_SONGS <= len(p["works"]) <= curate.MAX_SONGS
        assert p["title"] and len(p["title"]) <= 40
        assert p["description"] and p["description"].count(".") <= 3
        assert "lyric" not in p["description"].lower()
    everyone = [w for p in paths for w in p["works"]]
    assert len(everyone) == len(set(everyone)), "a song appears in two featured paths"


def test_committed_curated_paths_meet_the_rules_on_the_real_graph():
    if not config.GRAPH_DB.exists() or not (config.DATA / "audio" / "analysis.json").exists():
        pytest.skip("graph or preview analysis not built")
    from musichistory.paths import audio, graph as graph_mod, measured

    g = graph_mod.load(config.GRAPH_DB, config.DATA / "audio")
    m = measured.resolve_all(g, audio.load_cache(config.DATA / "audio" / "analysis.json"))
    for cp in curate.load_curated():
        assert curate.validate_curated(g, m, cp) == [], cp.id
        c = curate.curated_candidate(g, m, cp)
        assert c.terms["generic_hops"] <= 1, cp.id


def test_preview_trust_rejects_other_recordings():
    from musichistory.paths.graph import artist_similarity, preview_issue

    assert preview_issue("Earth Angel (Will You Be Mine)", "Will You Be Mine") == "title"
    assert preview_issue("(I Can't Get No) Satisfaction", "(I Can't Get No) Satisfaction (Mono)") == ""
    assert preview_issue("Whoomp! (There It Is)", "Whoomp! There It Is") == ""
    assert preview_issue("Be-Bop-A-Lula", "Be-Bop-a-Lula (Rerecorded Version)") == "re-recording"
    assert preview_issue("Angel of the Morning", "Angel of the Morning (Rockin' 2017 Version)") == "re-recording"
    assert preview_issue("Dynamite", "Dynamite (Holiday Remix)") == "remix"
    assert preview_issue("Macarena (Bayside Boys Mix)", "Macarena (Bayside Boys Remix)") == ""
    assert preview_issue("Signed, Sealed, Delivered I'm Yours", "Signed, Sealed, Delivered I'm Yours (Live)") == "live"
    assert preview_issue("Stayin' Alive", "Stayin' Alive") == ""
    assert preview_issue("Zombie", "Zombie (2025 Remastered)") == ""
    assert artist_similarity("Simon and Garfunkel", "Simon & Garfunkel") == 1.0
    assert artist_similarity("Janet", "Janet Jackson") == 1.0
    assert artist_similarity("Rob Thomas & Santana", "Escape the Fate") < curate.MIN_PREVIEW_ARTIST
    assert artist_similarity("Green Day", "Rockabye Baby!") < curate.MIN_PREVIEW_ARTIST
    s = song(1, 1960)
    s.preview_issue = "remix"
    assert not curate.trusted(s)


# ----------------------------------------------------------------------------- quality gate
def _gate_fixture(monkeypatch, extras=(), late=None):
    from types import SimpleNamespace as NS
    monkeypatch.setattr(curate, "_validation_extras", lambda: frozenset(extras))
    monkeypatch.setattr(curate, "_late_recordings", lambda: dict(late or {}))
    songs = {i: NS(title=f"Song {i}", work_id=f"W{i}") for i in (1, 2, 3)}
    graph = NS(songs=songs)

    def hop(a, b, semis=0, start=100.0, bpm=100.0, heard=(True, True)):
        edge = NS(source=a, target=b, evidence="axis progression I-V-vi-IV")
        checks = [NS(checked=True, in_key=h) for h in heard]
        return curate.Hop(edge, 0.85, "chord progression", semis, start, bpm, checks[0], checks[1], None)

    return graph, hop


def test_quality_gate_passes_a_smooth_audible_path(monkeypatch):
    graph, hop = _gate_fixture(monkeypatch)
    cand = curate.Candidate([1, 2, 3], [hop(1, 2, semis=2, start=110, bpm=100), hop(2, 3)])
    assert curate.quality_problems(graph, cand) == []


def test_quality_gate_rejects_each_problem(monkeypatch):
    graph, hop = _gate_fixture(monkeypatch, extras={"W2"}, late={"W3": 13})
    rough = curate.Candidate([1, 2, 3], [hop(1, 2, start=144, bpm=100), hop(2, 3, semis=5)])
    problems = " | ".join(curate.quality_problems(graph, rough))
    assert "Song 2 is a validation-control song" in problems
    assert "Song 3's recording is 13 years later" in problems
    assert "rough handoff into Song 2" in problems and "x1.44" in problems
    assert "rough handoff into Song 3" in problems and "+5 st" in problems


def test_quality_gate_requires_the_identity_to_be_audible(monkeypatch):
    graph, hop = _gate_fixture(monkeypatch)
    quiet = curate.Candidate([1, 2, 3], [hop(1, 2, heard=(False, False)), hop(2, 3, heard=(True, False))])
    assert any("audible in only 1 of 4" in p for p in curate.quality_problems(graph, quiet))
    half = curate.Candidate([1, 2, 3], [hop(1, 2, heard=(True, False)), hop(2, 3, heard=(True, False))])
    assert curate.quality_problems(graph, half) == []   # half the checks is enough
