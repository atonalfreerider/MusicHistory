"""Fixture graph databases for the Unity viewer, and a checker for the §10 contract.

Why this exists: the Unity loader reads ``data/graph/music_graph.db`` (docs/DESIGN.md §10)
with Unity's bundled SQLite 3.15. Before influence/layout produce a real graph, the viewer
needs a small, honest stand-in whose MIDI paths point at real files (so the walkthrough can
play something) and whose positions are NULL (so the loader's no-layout fallback is
exercised). ``check_graph`` is the same contract the loader relies on; running it against
any graph DB (fixture, demo or real) catches schema drift before Unity does.

The fixture's songs are neutral ("Fixture 01" ... by "Test MIDI 01" ...): the MIDI files come
from the research prototypes' test set and the influence edges are invented, so no real
title is paired with a made-up influence claim. MIDI files are copied through
``musichistory.midi.process`` (the pipeline's sanitizer), which drops lyric, text, marker and
other free-text events; no lyric text is ever decoded, stored or printed here.

CLI::

    python tests/unity/graph_fixture.py build [--out data/fixtures/unity] [--source <dir with MIDIs>]
    python tests/unity/graph_fixture.py check <graph.db> [...]
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "data" / "fixtures" / "unity"
DEFAULT_SOURCE = Path(
    r"C:\Users\johnb\AppData\Local\Temp\claude\C--Users-johnb-Desktop-MusicHistory"
    r"\fdf70476-fc2d-494c-ab1e-43c07c356046\scratchpad\pp-test"
)

# docs/DESIGN.md §10, verbatim apart from comments. SQLite 3.15 compatible.
SCHEMA = """
CREATE TABLE graph_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE nodes(id INTEGER PRIMARY KEY,
  position_x REAL, position_y REAL, position_z REAL);
CREATE TABLE song_node(
  node_id INTEGER PRIMARY KEY,
  work_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, artist TEXT NOT NULL,
  year INTEGER NOT NULL, release_date TEXT, date_precision INTEGER,
  time_value REAL NOT NULL, canon_rank INTEGER,
  tonic_pc INTEGER NOT NULL, mode TEXT NOT NULL, key_name TEXT NOT NULL,
  norm_shift INTEGER NOT NULL,
  native_bpm REAL NOT NULL, beats_per_bar REAL NOT NULL, first_downbeat REAL NOT NULL,
  midi_path TEXT NOT NULL,
  normalized_midi_path TEXT,
  midi_source TEXT,
  excerpt_start_beat REAL NOT NULL, excerpt_end_beat REAL NOT NULL,
  tree_parent_node INTEGER, tree_root_node INTEGER NOT NULL, tree_depth INTEGER NOT NULL,
  ref_count INTEGER NOT NULL, ref_norm REAL, katz REAL, descendants INTEGER NOT NULL,
  in_degree INTEGER NOT NULL, out_degree INTEGER NOT NULL,
  key_confidence REAL, melody_confidence REAL,
  main_loop TEXT,
  summary TEXT
);
CREATE TABLE influence_edges(
  id INTEGER PRIMARY KEY,
  source_node INTEGER NOT NULL, target_node INTEGER NOT NULL,
  kind TEXT NOT NULL,
  channels TEXT NOT NULL,
  primary_channel TEXT NOT NULL,
  score_bits REAL NOT NULL, z REAL NOT NULL, q REAL,
  similarity REAL NOT NULL,
  weight REAL NOT NULL,
  src_start_beat REAL, src_end_beat REAL, dst_start_beat REAL, dst_end_beat REAL,
  evidence TEXT,
  UNIQUE(source_node, target_node), CHECK(source_node < target_node));
CREATE TABLE node_layout_metadata(node_id INTEGER PRIMARY KEY, mass REAL, is_root INTEGER,
  tree_depth INTEGER, display_radius REAL, time_axis_value REAL);
CREATE TABLE layout_run(run_id INTEGER PRIMARY KEY, created_at TEXT, device TEXT,
  iterations INTEGER, loop_ms REAL, final_mean_move REAL, time_axis TEXT, time_direction TEXT,
  year_scale REAL, min_time REAL, params_json TEXT);
"""

REQUIRED_COLUMNS = {
    "graph_meta": {"key", "value"},
    "nodes": {"id", "position_x", "position_y", "position_z"},
    "song_node": {
        "node_id", "work_id", "title", "artist", "year", "release_date", "date_precision",
        "time_value", "canon_rank", "tonic_pc", "mode", "key_name", "norm_shift", "native_bpm",
        "beats_per_bar", "first_downbeat", "midi_path", "normalized_midi_path", "midi_source",
        "excerpt_start_beat", "excerpt_end_beat", "tree_parent_node", "tree_root_node",
        "tree_depth", "ref_count", "ref_norm", "katz", "descendants", "in_degree", "out_degree",
        "key_confidence", "melody_confidence", "main_loop", "summary",
    },
    "influence_edges": {
        "id", "source_node", "target_node", "kind", "channels", "primary_channel", "score_bits",
        "z", "q", "similarity", "weight", "src_start_beat", "src_end_beat", "dst_start_beat",
        "dst_end_beat", "evidence",
    },
}
CHANNELS = {"melody", "bass", "chord", "loop"}

# Sharps/flats per DESIGN.md §3.
_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
_FLAT_MAJOR = {5, 10, 3, 8, 1, 6}      # F Bb Eb Ab Db Gb
_FLAT_MINOR = {2, 7, 0, 5, 10, 3}      # D G C F Bb Eb


def key_name(tonic_pc: int, minor: bool) -> str:
    flats = (tonic_pc in _FLAT_MINOR) if minor else (tonic_pc in _FLAT_MAJOR)
    return f"{(_FLAT if flats else _SHARP)[tonic_pc % 12]} {'minor' if minor else 'major'}"


# --------------------------------------------------------------------------- checker
def check_graph(db_path: Path) -> list[str]:
    """Problems that would break the Unity loader or violate §10 (empty list = conformant)."""
    problems: list[str] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        schema = {r["name"]: (r["sql"] or "") for r in conn.execute("SELECT name, sql FROM sqlite_master")}
        for table, cols in REQUIRED_COLUMNS.items():
            if table not in schema:
                problems.append(f"missing table {table}")
                continue
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if cols - have:
                problems.append(f"{table} lacks columns {sorted(cols - have)}")
        if problems:
            return problems
        # SQLite 3.15: no STRICT tables, generated columns, UPSERT/window functions in views or triggers.
        for name, sql in schema.items():
            up = sql.upper()
            if ") STRICT" in up or "GENERATED ALWAYS" in up:
                problems.append(f"{name}: uses a feature newer than SQLite 3.15")
            kind = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            if kind in ("view", "trigger") and (" OVER " in up or "ON CONFLICT" in up):
                problems.append(f"{name}: window function or UPSERT inside a {kind}")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "delete":
            problems.append(f"journal_mode is {mode}, expected delete")

        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM graph_meta")}
        songs = [dict(r) for r in conn.execute("SELECT * FROM song_node ORDER BY node_id")]
        node_ids = [r[0] for r in conn.execute("SELECT id FROM nodes ORDER BY id")]
        edges = [dict(r) for r in conn.execute("SELECT * FROM influence_edges ORDER BY id")]
        n = len(songs)
        if [s["node_id"] for s in songs] != list(range(1, n + 1)):
            problems.append("song_node ids are not contiguous 1..N")
        if node_ids != list(range(1, n + 1)):
            problems.append("nodes ids do not match song_node ids 1..N")
        if problems:
            return problems
        by_id = {s["node_id"]: s for s in songs}
        for a, b in zip(songs, songs[1:]):
            if (a["time_value"], a["work_id"]) > (b["time_value"], b["work_id"]):
                problems.append(f"node {b['node_id']} is not ordered by (time_value, work_id)")
                break

        tree_in: dict[int, list[int]] = {}
        indeg = dict.fromkeys(by_id, 0)
        outdeg = dict.fromkeys(by_id, 0)
        for e in edges:
            s, t = e["source_node"], e["target_node"]
            if s not in by_id or t not in by_id:
                problems.append(f"edge {e['id']} references a missing node")
                continue
            indeg[t] += 1
            outdeg[s] += 1
            if not s < t:
                problems.append(f"edge {e['id']}: source_node >= target_node")
            if not by_id[s]["time_value"] < by_id[t]["time_value"]:
                problems.append(f"edge {e['id']}: source is not earlier than target")
            if e["kind"] not in ("tree", "secondary"):
                problems.append(f"edge {e['id']}: kind {e['kind']!r}")
            if e["primary_channel"] not in CHANNELS:
                problems.append(f"edge {e['id']}: primary_channel {e['primary_channel']!r}")
            chans = {c.strip() for c in (e["channels"] or "").split(",") if c.strip()}
            if not chans or not chans <= CHANNELS or e["primary_channel"] not in chans:
                problems.append(f"edge {e['id']}: channels {e['channels']!r}")
            if not 0.0 <= e["similarity"] <= 1.0:
                problems.append(f"edge {e['id']}: similarity out of 0..1")
            if e["kind"] == "tree":
                tree_in.setdefault(t, []).append(s)

        children: dict[int, list[int]] = {i: [] for i in by_id}
        for s in songs:
            nid, parent = s["node_id"], s["tree_parent_node"]
            got = tree_in.get(nid, [])
            if parent is None:
                if got:
                    problems.append(f"root {nid} has a tree edge")
                if s["tree_root_node"] != nid or s["tree_depth"] != 0:
                    problems.append(f"root {nid}: tree_root_node/tree_depth inconsistent")
            else:
                if got != [parent]:
                    problems.append(f"node {nid}: tree edges {got}, expected exactly one from {parent}")
                if parent in children:
                    children[parent].append(nid)
            if s["in_degree"] != indeg[nid] or s["out_degree"] != outdeg[nid]:
                problems.append(f"node {nid}: in/out degree {s['in_degree']}/{s['out_degree']} "
                                f"!= edges {indeg[nid]}/{outdeg[nid]}")
            if not s["excerpt_end_beat"] > s["excerpt_start_beat"]:
                problems.append(f"node {nid}: empty excerpt")
            mp = s["midi_path"] or ""
            if "\\" in mp or Path(mp).is_absolute() or mp.startswith("/"):
                problems.append(f"node {nid}: midi_path must be relative with '/' separators")
            if not 0 <= s["tonic_pc"] <= 11 or s["mode"] not in ("major", "minor"):
                problems.append(f"node {nid}: key {s['tonic_pc']} {s['mode']}")
            if s["native_bpm"] <= 0 or s["beats_per_bar"] <= 0:
                problems.append(f"node {nid}: bpm/beats_per_bar not positive")

        # Depth, root and descendant counts must agree with the parent pointers.
        def walk(nid: int, depth: int, root: int) -> int:
            s = by_id[nid]
            if s["tree_depth"] != depth or s["tree_root_node"] != root:
                problems.append(f"node {nid}: depth/root {s['tree_depth']}/{s['tree_root_node']} != {depth}/{root}")
            total = 0
            for c in children[nid]:
                total += 1 + walk(c, depth + 1, root)
            if s["descendants"] != total:
                problems.append(f"node {nid}: descendants {s['descendants']} != {total}")
            return total

        sys.setrecursionlimit(max(10000, n * 2 + 100))
        roots = [s["node_id"] for s in songs if s["tree_parent_node"] is None]
        seen = 0
        for r in roots:
            seen += 1 + walk(r, 0, r)
        if seen != n:
            problems.append(f"tree covers {seen} of {n} nodes (cycle or dangling parent)")

        for key, want in (("song_count", n), ("edge_count", len(edges)), ("root_count", len(roots))):
            if key in meta and meta[key] is not None and int(meta[key]) != want:
                problems.append(f"graph_meta {key}={meta[key]} but table has {want}")
        if meta.get("schema_version") not in ("1", 1):
            problems.append(f"graph_meta schema_version={meta.get('schema_version')!r}")
    finally:
        conn.close()
    return problems


# --------------------------------------------------------------------------- fixture
@dataclass
class FixtureSong:
    source: str        # MIDI path relative to the source folder
    year: int
    month: int


# The research prototypes' test MIDIs (lyric test files are deliberately left out).
FIXTURE_SONGS = [
    FixtureSong("SongLibrary-cd1ce734/source.mid", 1958, 3),
    FixtureSong("Ticket-to-Ride---The-Beatles/aligned.mid", 1965, 4),
    FixtureSong("ComeFirst-prepared-stems/aligned.mid", 1972, 6),
    FixtureSong("Drank-prepared-stems/aligned.mid", 1979, 2),
    FixtureSong("JustAnotherInterlude-prepared-stems/aligned.mid", 1986, 9),
    FixtureSong("TouchxBeMyBaby-prepared-stems/aligned.mid", 1994, 1),
    FixtureSong("Rollout---Ludacris-6fc7091b-f38e10/aligned.mid", 2001, 10),
    FixtureSong("Sugar-Were-Going-Down---Fall-Out-Boy-e10b633d-e0c0ea/aligned.mid", 2005, 6),
    FixtureSong("Umbrella-bf6a6793-5b304c/aligned.mid", 2007, 3),
    FixtureSong("Fireflies---Owl-City-f259424e-61b3f5/aligned.mid", 2009, 7),
    FixtureSong("Sexual-prepared-stems/aligned.mid", 2012, 5),
    FixtureSong("Sorry-1cc880fe-8803a9/aligned.mid", 2015, 10),
    FixtureSong("SaySo-prepared-stems/aligned.mid", 2019, 11),
]
# Invented structure: node -> tree parent (1-based; None = root), plus secondary edges.
FIXTURE_PARENTS = [None, 1, 1, 2, None, 4, 2, 6, 5, 8, 7, 9, 10]
FIXTURE_SECONDARY = [(3, 7), (4, 8), (1, 9), (5, 12), (2, 6), (6, 13)]
_CHANNEL_CYCLE = [("melody", "melody"), ("chord", "chord"), ("bass", "bass"),
                  ("loop,chord", "loop"), ("melody,chord", "melody")]
_LOOPS = ["I-V-vi-IV", "vi-IV-I-V (i-VI-III-VII)", "I-vi-IV-V", "I-IV-V-IV", None]


def _time_signature(features: dict) -> float:
    rows = features.get("time_signatures") or []
    if rows:
        _, num, den = rows[0][:3]
        return float(num) * 4.0 / float(den)
    return 4.0


def _key(features: dict) -> tuple[int, bool]:
    for _, sf, minor in features.get("key_signatures") or []:
        pc = (int(sf) * 7) % 12
        return ((pc + 9) % 12, True) if minor else (pc, False)
    return 0, False


def build_fixture(out_dir: Path = DEFAULT_OUT, source_dir: Path = DEFAULT_SOURCE) -> Path:
    """Write ``<out>/graph/fixture_graph.db`` and ``<out>/songs/<work_id>/score.mid``."""
    from musichistory import midi as mh_midi

    graph_dir = out_dir / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    db_path = graph_dir / "fixture_graph.db"
    if db_path.exists():
        db_path.unlink()

    rows = []
    for i, fs in enumerate(FIXTURE_SONGS, start=1):
        src = source_dir / fs.source
        processed = mh_midi.process(src.read_bytes())
        if processed.data is None or processed.features is None:
            raise RuntimeError(f"fixture MIDI {src.name} failed sanitization: {processed.reason}")
        feats = processed.features
        work_id = "R" + hashlib.sha1(f"fixture {i:02d}|test midi".encode()).hexdigest()[:12]
        song_dir = out_dir / "songs" / work_id
        song_dir.mkdir(parents=True, exist_ok=True)
        (song_dir / "score.mid").write_bytes(processed.data)
        tonic, minor = _key(feats)
        bpb = _time_signature(feats)
        end_beat = float(feats["end_beat"])
        bars = max(1, int(end_beat // bpb))
        start_bar = 4 if bars >= 24 else 0
        n_bars = max(1, min(16, bars - start_bar))
        bpm = float((feats.get("tempo") or {}).get("median_bpm") or 120.0)
        target = 9 if minor else 0
        rows.append(dict(
            node_id=i, work_id=work_id, title=f"Fixture {i:02d}", artist=f"Test MIDI {i:02d}",
            year=fs.year, release_date=f"{fs.year}-{fs.month:02d}", date_precision=10,
            time_value=fs.year + (fs.month - 0.5) / 12.0, canon_rank=i * 7,
            tonic_pc=tonic, mode="minor" if minor else "major", key_name=key_name(tonic, minor),
            norm_shift=((target - tonic + 5) % 12) - 5, native_bpm=round(bpm, 2), beats_per_bar=bpb,
            first_downbeat=0.0, midi_path=f"../songs/{work_id}/score.mid", normalized_midi_path=None,
            midi_source="fixture", excerpt_start_beat=start_bar * bpb,
            excerpt_end_beat=(start_bar + n_bars) * bpb,
            tree_parent_node=FIXTURE_PARENTS[i - 1], key_confidence=0.9, melody_confidence=0.8,
            main_loop=_LOOPS[i % len(_LOOPS)], summary=f"fixture song; {feats['n_notes']} notes",
        ))

    n = len(rows)
    children: dict[int, list[int]] = {r["node_id"]: [] for r in rows}
    for r in rows:
        if r["tree_parent_node"]:
            children[r["tree_parent_node"]].append(r["node_id"])

    def fill(nid: int, depth: int, root: int) -> int:
        r = rows[nid - 1]
        r["tree_depth"], r["tree_root_node"] = depth, root
        r["descendants"] = sum(1 + fill(c, depth + 1, root) for c in children[nid])
        return r["descendants"]

    for r in rows:
        if r["tree_parent_node"] is None:
            fill(r["node_id"], 0, r["node_id"])

    edges = [(r["tree_parent_node"], r["node_id"], "tree") for r in rows if r["tree_parent_node"]]
    edges += [(s, t, "secondary") for s, t in FIXTURE_SECONDARY]
    edges.sort(key=lambda e: (e[1], e[0]))
    for r in rows:
        r["in_degree"] = sum(1 for e in edges if e[1] == r["node_id"])
        r["out_degree"] = sum(1 for e in edges if e[0] == r["node_id"])
        r["ref_count"] = r["out_degree"]
        later = n - r["node_id"]
        r["ref_norm"] = round(r["ref_count"] / later, 6) if later else 0.0
        r["katz"] = round(r["descendants"] * 0.2 + r["out_degree"], 6)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript(SCHEMA)
        cols = list(rows[0].keys()) + ["tree_depth", "tree_root_node", "descendants", "in_degree",
                                       "out_degree", "ref_count", "ref_norm", "katz"]
        cols = list(dict.fromkeys(cols))
        conn.executemany(
            f"INSERT INTO song_node({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            [[r[c] for c in cols] for r in rows])
        # Layout has not run: positions stay NULL (the loader's fallback layout is exercised).
        conn.executemany("INSERT INTO nodes(id) VALUES(?)", [(r["node_id"],) for r in rows])
        for k, (s, t, kind) in enumerate(edges, start=1):
            channels, primary = _CHANNEL_CYCLE[k % len(_CHANNEL_CYCLE)]
            sim = round(0.45 + 0.5 * ((k * 37) % 11) / 10.0, 3)
            src, dst = rows[s - 1], rows[t - 1]
            conn.execute(
                "INSERT INTO influence_edges(id, source_node, target_node, kind, channels, primary_channel,"
                " score_bits, z, q, similarity, weight, src_start_beat, src_end_beat, dst_start_beat,"
                " dst_end_beat, evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (k, s, t, kind, channels, primary, round(20 + 40 * sim, 2), round(3 + 5 * sim, 3),
                 0.01, min(1.0, sim), round(0.5 + sim / 2, 3), src["excerpt_start_beat"],
                 src["excerpt_end_beat"], dst["excerpt_start_beat"], dst["excerpt_end_beat"],
                 f"fixture evidence: {primary} {int(20 + 40 * sim)} bits"))
        meta = {
            "schema_version": "1",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "normalization": "relative", "target_key": "C major / A minor", "target_bpm": "120",
            "min_time": str(rows[0]["time_value"]), "max_time": str(rows[-1]["time_value"]),
            "song_count": str(n), "edge_count": str(len(edges)),
            "root_count": str(sum(1 for r in rows if r["tree_parent_node"] is None)),
            "resonance_commit": None, "pipeline_commit": None,
            "midi_base": "relative to this file's folder", "synthetic": "1", "fixture": "unity",
        }
        conn.executemany("INSERT INTO graph_meta(key, value) VALUES(?, ?)", list(meta.items()))
        conn.commit()
    finally:
        conn.close()
    return db_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, default=DEFAULT_OUT)
    b.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    c = sub.add_parser("check")
    c.add_argument("db", type=Path, nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        path = build_fixture(args.out, args.source)
        problems = check_graph(path)
        print(f"wrote {path} ({len(FIXTURE_SONGS)} songs); problems: {len(problems)}")
        for p in problems[:20]:
            print("  " + p)
        return 1 if problems else 0
    rc = 0
    for db in args.db:
        problems = check_graph(db)
        print(f"{db}: {'OK' if not problems else f'{len(problems)} problems'}")
        for p in problems[:20]:
            print("  " + p)
        rc |= bool(problems)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
