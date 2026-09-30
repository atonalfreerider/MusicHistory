"""Ground truth for validating the influence graph (never used to build it).

Two sources, both written to ``known_influence(src_work_id, dst_work_id, kind, note)``:

* Wikidata links among works: P144 "based on" (samples, interpolations, adaptations) as
  ``wikidata_P144`` and P2550 "recording or performance of" between two *different* works
  as ``wikidata_P2550`` (a P2550 inside one work is a merge, not an influence).
* ``controls.json``, the MIR report's validation controls: positive pairs and the
  Pachelbel cluster (``control_positive``), commonplace families and court cases
  (``control_negative``), and cover/version pairs (``control_version``; when both sides
  resolve to the same work the canon merged them, and the row has src = dst).

Controls are matched by normalized title (``resolve.title_keys``) plus artist
(``resolve.same_artist`` against any performer of the work), best-scoring work first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from .resolve import same_artist, title_keys

CONTROLS_PATH = Path(__file__).with_name("controls.json")


@dataclass
class WorkKeys:
    work_id: str
    titles: list[str]
    artists: list[str]
    score: float = 0.0
    year: int | None = None


@dataclass
class ControlMatch:
    group: str
    title: str
    artist: str
    year: int | None
    work_id: str | None


@dataclass
class ControlIndex:
    by_title: dict[str, list[WorkKeys]] = field(default_factory=dict)
    years: dict[str, int | None] = field(default_factory=dict)

    @classmethod
    def build(cls, works: list[WorkKeys]) -> "ControlIndex":
        idx: dict[str, list[WorkKeys]] = {}
        for w in works:
            keys = {k for t in w.titles for k in title_keys(t)}
            for k in keys:
                idx.setdefault(k, []).append(w)
        return cls(idx, {w.work_id: w.year for w in works})

    def order_key(self, song: dict, work_id: str) -> tuple[int, str]:
        """Undirected control groups are written earlier -> later by the works' own years."""
        y = self.years.get(work_id)
        return (y if y is not None else song.get("year") or 0, song["title"])

    def find(self, title: str, artist: str) -> str | None:
        cands: dict[str, WorkKeys] = {}
        for k in title_keys(title):
            for w in self.by_title.get(k, ()):
                cands[w.work_id] = w
        hits = [w for w in cands.values() if any(same_artist(a, artist) for a in w.artists if a)]
        return max(hits, key=lambda w: w.score).work_id if hits else None


def load_controls(path: Path = CONTROLS_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _label(song: dict) -> str:
    return f"{song['title']} / {song['artist']} / {song.get('year')}"


def control_rows(controls: dict, index: ControlIndex) -> tuple[list[tuple[str, str, str, str]], list[ControlMatch]]:
    """(known_influence rows, how every control song resolved)."""
    rows: dict[tuple[str, str, str], dict] = {}
    matches: list[ControlMatch] = []

    def look(group: str, song: dict) -> str | None:
        wid = index.find(song["title"], song["artist"])
        matches.append(ControlMatch(group, song["title"], song["artist"], song.get("year"), wid))
        return wid

    def add(src: str, dst: str, kind: str, note: dict) -> None:
        key = (src, dst, kind)
        if key in rows:
            groups = rows[key].setdefault("also", [])
            groups.append(note.get("group"))
        else:
            rows[key] = note

    for pair in controls.get("positive", []):
        s, d = look("positive", pair["src"]), look("positive", pair["dst"])
        if s and d and s != d:
            add(s, d, "control_positive", {"group": "positive", "src": _label(pair["src"]), "dst": _label(pair["dst"]),
                                           "channel": pair.get("channel"), "note": pair.get("note")})
    cluster = controls.get("pachelbel_cluster", {})
    members = [(song, look("pachelbel_cluster", song)) for song in cluster.get("songs", [])]
    members = sorted([m for m in members if m[1]], key=lambda m: index.order_key(*m))
    for (a, wa), (b, wb) in combinations(members, 2):
        if wa != wb:
            add(wa, wb, "control_positive", {"group": "pachelbel_cluster", "src": _label(a), "dst": _label(b)})
    for family, songs in controls.get("negative", {}).get("families", {}).items():
        found = [(s, look(f"negative:{family}", s)) for s in songs]
        found = sorted([f for f in found if f[1]], key=lambda f: index.order_key(*f))
        for (a, wa), (b, wb) in combinations(found, 2):
            if wa != wb:
                add(wa, wb, "control_negative", {"group": family, "src": _label(a), "dst": _label(b)})
    for pair in controls.get("negative", {}).get("pairs", []):
        s, d = look("negative:court", pair["src"]), look("negative:court", pair["dst"])
        if s and d and s != d:
            add(s, d, "control_negative", {"group": "court", "src": _label(pair["src"]), "dst": _label(pair["dst"]),
                                           "note": pair.get("note")})
    for pair in controls.get("version", []):
        a, b = look("version", pair["a"]), look("version", pair["b"])
        if a and b:
            add(a, b, "control_version", {"group": "version", "src": _label(pair["a"]), "dst": _label(pair["b"]),
                                          "merged": a == b})
    out = [(s, d, k, json.dumps(n, separators=(",", ":"), ensure_ascii=False)) for (s, d, k), n in rows.items()]
    return out, matches


def wikidata_rows(work_items: dict[str, set[str]], entities: dict[str, dict],
                  work_of: dict[str, str]) -> list[tuple[str, str, str, str]]:
    """P144 / cross-work P2550 links between works in the table. ``work_items``: work -> its item QIDs."""
    works = set(work_items)
    item_work = {q: w for w, qs in work_items.items() for q in qs}
    rows: dict[tuple[str, str, str], str] = {}
    for w, items in work_items.items():
        for q in items:
            ent = entities.get(q) or {}
            for prop, kind in (("P144", "wikidata_P144"), ("P2550", "wikidata_P2550")):
                for t in ent.get(prop, []):
                    tw = item_work.get(t) or work_of.get(t, t)
                    if tw in works and tw != w:
                        rows[(tw, w, kind)] = json.dumps({"item": q, "prop": prop, "target": t}, separators=(",", ":"))
    return [(s, d, k, n) for (s, d, k), n in rows.items()]
