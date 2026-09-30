"""The identity-lineage graph as the path curation needs it (DESIGN §8b, §10): songs, edges
(tree and secondary, earlier -> later) with the shared family, and which songs have a preview."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path


@dataclass
class Song:
    node_id: int
    work_id: str
    title: str
    artist: str
    year: int
    time_value: float
    canon_rank: int | None
    tonic_pc: int
    mode: str
    key_name: str
    native_bpm: float
    beats_per_bar: float
    preview: Path | None = None
    preview_match: float | None = None       # manifest match score
    preview_artist: float | None = None      # similarity of the iTunes artist to ours
    preview_label: str = ""                  # "track - artist" as matched
    preview_issue: str = ""                  # why the matched recording is not the song's (empty: fine)


@dataclass
class Family:
    family_id: int
    label: str
    kind: str                                # loop | schema | progression | strong
    roman: str | None
    size: int


@dataclass
class Edge:
    source: int
    target: int
    kind: str                                # tree | secondary
    evidence: str
    z: float
    score_bits: float
    family: Family | None = None

    @property
    def strong(self) -> bool:
        return (self.family is not None and self.family.kind == "strong") or self.z > 0


@dataclass
class Graph:
    songs: dict[int, Song]
    edges: dict[tuple[int, int], Edge]
    families: dict[int, Family]
    out: dict[int, list[Edge]] = field(default_factory=dict)
    by_work: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.out = {}
        for e in self.edges.values():
            self.out.setdefault(e.source, []).append(e)
        self.by_work = {s.work_id: nid for nid, s in self.songs.items()}

    def edge(self, a: int, b: int) -> Edge | None:
        return self.edges.get((a, b))


def _norm(s: str) -> str:
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", s.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", s)).strip()


def _lead(a: str) -> str:
    return re.split(r"\s+(?:with|feat\.?|featuring|&|and his|and her|and the|x)\s+", a, flags=re.I)[0]


def artist_similarity(ours: str, theirs: str) -> float:
    """1.0 when either lead artist is named in the other credit ("Janet" / "Janet Jackson",
    "Simon and Garfunkel" / "Simon & Garfunkel"), else the best string similarity of the lead
    artists or of the full credits."""
    a, b = _norm(ours.replace("&", " and ")), _norm(theirs.replace("&", " and "))
    la, lb = _norm(_lead(ours)), _norm(_lead(theirs))
    if (la and re.search(rf"\b{re.escape(la)}\b", b)) or (lb and re.search(rf"\b{re.escape(lb)}\b", a)):
        return 1.0
    return max(SequenceMatcher(None, la, lb).ratio(), SequenceMatcher(None, a, b).ratio())


def _full(s: str) -> str:
    """Normalized title keeping parenthesized parts."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", re.sub(r"[\(\)\[\]]", " ", s.lower()))).strip()


def preview_issue(title: str, track: str) -> str:
    """Why the matched iTunes track is probably not the song's recording ('' = looks right):
    another title (the main title neither contained nor similar), a re-recording, a remix
    (unless the song itself is a mix) or a live take."""
    main = _norm(title) or _full(title)
    if main not in _full(track) and SequenceMatcher(None, main, _norm(track) or _full(track)).ratio() < 0.8:
        return "title"
    t = track.lower()
    if re.search(r"re-?record|rockin' \d{4} version", t):
        return "re-recording"
    if re.search(r"\bremix\b", t) and not re.search(r"\b(re)?mix\b", title.lower()):
        return "remix"
    if re.search(r"[\(\[]live\b|\blive (from|at)\b", t):
        return "live"
    return ""


def load(db_path: Path, audio_dir: Path) -> Graph:
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        songs = {}
        for r in conn.execute("SELECT * FROM song_node"):
            songs[r["node_id"]] = Song(r["node_id"], r["work_id"], r["title"], r["artist"], int(r["year"]),
                                       float(r["time_value"]), r["canon_rank"], int(r["tonic_pc"]), r["mode"],
                                       r["key_name"], float(r["native_bpm"] or 0.0), float(r["beats_per_bar"] or 4.0))
        families = {r["family_id"]: Family(r["family_id"], r["label"], r["kind"], r["roman"], int(r["size"]))
                    for r in conn.execute("SELECT * FROM identity_family")} if _has(conn, "identity_family") else {}
        member: dict[int, set[int]] = {}
        if _has(conn, "song_family"):
            for r in conn.execute("SELECT node_id, family_id FROM song_family"):
                member.setdefault(r["node_id"], set()).add(r["family_id"])
        edges = {}
        for r in conn.execute("SELECT source_node, target_node, kind, evidence, z, score_bits FROM influence_edges"):
            e = Edge(r["source_node"], r["target_node"], r["kind"], r["evidence"] or "", float(r["z"] or 0.0),
                     float(r["score_bits"] or 0.0))
            shared = member.get(e.source, set()) & member.get(e.target, set())
            fam = [families[f] for f in shared if families[f].label == e.evidence]
            e.family = fam[0] if fam else None
            edges[(e.source, e.target)] = e
    finally:
        conn.close()
    manifest = {}
    mpath = Path(audio_dir) / "manifest.json"
    if mpath.exists():
        manifest = {m["work_id"]: m for m in json.loads(mpath.read_text(encoding="utf-8"))}
    for s in songs.values():
        p = Path(audio_dir) / s.work_id / "preview.mp3"
        if p.exists():
            s.preview = p
            m = manifest.get(s.work_id) or {}
            s.preview_match = m.get("match_score")
            if m.get("itunes_artist"):
                s.preview_artist = round(artist_similarity(s.artist, m["itunes_artist"]), 3)
                s.preview_label = f"{m.get('itunes_track')} - {m.get('itunes_artist')}"
                s.preview_issue = preview_issue(s.title, m.get("itunes_track") or "")
    return Graph(songs, edges, families)


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
