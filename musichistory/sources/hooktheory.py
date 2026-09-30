"""Hooktheory annotations (Sheet Sage release): reference data for select, not MIDI.

One 20 MB file (CC BY-NC-SA 3.0) with 26,175 key-relative melody/harmony annotations of
song *sections*. hooktheory.com itself is never contacted (its robots.txt disallows AI
agents); the file comes from the Sheet Sage data repository on GitHub and is verified by
SHA-256. Entries are keyed by artist/song slugs; fetch matches them to works with the
same thresholds as MIDI files and records the pairs in ``hooktheory_match`` so select can
load the sections of a work without re-matching.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from functools import lru_cache
from pathlib import Path

from .. import config
from ..http import download
from ..textnorm import squash_loose, title_core, title_key, title_matches
from . import base
from .base import ARTIST_MIN, TITLE_MIN, Work

URL = "https://github.com/chrisdonahue/sheetsage-data/raw/refs/heads/main/hooktheory/Hooktheory.json.gz"
SHA256 = "917b7cd58f5f4e07d6c36acf7bfad958c99ee05472dab3555399141094698e0c"


def path() -> Path:
    return config.CACHE / "hooktheory" / "Hooktheory.json.gz"


def ensure_file(offline: bool = False) -> Path:
    p = path()
    if not p.exists():
        if offline:
            raise FileNotFoundError(p)
        download(URL, p)
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    if h != SHA256:
        bad = p.with_suffix(".bad")
        p.replace(bad)
        raise ValueError(f"Hooktheory.json.gz SHA-256 mismatch ({h}); moved to {bad}")
    return p


@lru_cache(maxsize=1)
def load(offline: bool = True) -> dict[str, dict]:
    with gzip.open(ensure_file(offline), "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _unslug(s: str) -> str:
    return (s or "").replace("-", " ")


class HooktheoryIndex:
    def __init__(self, data: dict[str, dict]) -> None:
        self.data = data
        self.by_title: dict[str, list[str]] = {}
        for ht_id, e in data.items():
            song = _unslug(e.get("hooktheory", {}).get("song", ""))
            for key in {squash_loose(title_key(song)), squash_loose(title_core(song))}:  # as Work.title_forms
                if key:
                    self.by_title.setdefault(key, []).append(ht_id)

    def match(self, work: Work) -> list[tuple[str, float, float]]:
        ids: set[str] = set()
        for t in work.title_forms:
            ids.update(self.by_title.get(t, ()))
        out = []
        for ht_id in sorted(ids):
            h = self.data[ht_id].get("hooktheory", {})
            ts = title_matches(_unslug(h.get("song", "")), work.title)
            ars = max(base.artist_score(_unslug(h.get("artist", "")), a) for a in work.artists)
            if ts >= TITLE_MIN and ars >= ARTIST_MIN:
                out.append((ht_id, ts, ars))
        return out


def match_works(conn: sqlite3.Connection, works: list[Work], offline: bool = False) -> tuple[int, int]:
    """Record Hooktheory sections for every work; returns (works matched, sections)."""
    idx = HooktheoryIndex(load(offline))
    n_works = n_sections = 0
    for w in works:
        rows = idx.match(w)
        conn.execute("DELETE FROM hooktheory_match WHERE work_id=?", (w.work_id,))
        conn.executemany("INSERT INTO hooktheory_match(work_id, ht_id, title_score, artist_score) VALUES (?,?,?,?)",
                         [(w.work_id, i, ts, ars) for i, ts, ars in rows])
        n_works += bool(rows)
        n_sections += len(rows)
    conn.commit()
    return n_works, n_sections


def sections_for(conn: sqlite3.Connection, work_id: str) -> list[dict]:
    """Hooktheory entries (full JSON records: annotations, alignment, ...) for a work."""
    ids = [r[0] for r in conn.execute("SELECT ht_id FROM hooktheory_match WHERE work_id=? ORDER BY ht_id", (work_id,))]
    if not ids:
        return []
    data = load()
    return [dict(data[i], id=i) for i in ids if i in data]
