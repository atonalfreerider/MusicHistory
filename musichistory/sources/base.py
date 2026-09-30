"""Shared pieces of every MIDI source: works, name matching, candidate ingestion.

Matching (DESIGN.md §5): a file name is read in several ways ("Artist - Title",
"Title - Artist", "<artist dir>/Title", "artisttitle2" squashed 8.3 names, ...) and each
reading is scored with ``musichistory.textnorm``. A name is **accepted** when one reading
has title >= 92 and artist >= 85 against any of the work's ``search_artists``. A name whose
title matches but whose artist is missing is ``title_only``; one that names a different
artist is ``other_artist`` (usually a cover by someone else). Both are rejected here; the
Lakh adapter may still promote a title match to ``probable`` via lmd_matched.

Ingestion: the MD5 of the original bytes is the identity. A file already stored for the
work (from any source) is a duplicate and is not stored again. Everything else goes
through ``musichistory.midi.process`` and becomes one ``candidate`` row; valid ones are
written to ``data/candidates/<work_id>/<source>__<md5>.mid``. Raw downloads are never
kept on disk (they may contain lyrics); only sanitized files are.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from rapidfuzz import fuzz

from .. import config, midi
from ..textnorm import (
    artist_key,
    artist_matches,
    artist_tokens,
    fold,
    primary_artist,
    squash,
    title_core,
    title_key,
    title_matches,
)

TITLE_MIN = 92.0
ARTIST_MIN = 85.0
MAX_PER_SOURCE = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_attempt(      -- one row per (work, source, file) tried
  work_id TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT NOT NULL,
  status TEXT NOT NULL,     -- 'new' | 'duplicate' | 'invalid' | 'download_failed' | 'rejected'
  md5 TEXT, bytes INTEGER, detail TEXT, attempted_at TEXT,
  PRIMARY KEY(work_id, source, source_ref));
CREATE TABLE IF NOT EXISTS fetch_status(       -- one row per (work, source) searched
  work_id TEXT NOT NULL, source TEXT NOT NULL,
  searched_at TEXT, n_matches INTEGER, n_rejected INTEGER, error TEXT,
  PRIMARY KEY(work_id, source));
CREATE TABLE IF NOT EXISTS hooktheory_match(   -- Hooktheory annotations of a work (select)
  work_id TEXT NOT NULL, ht_id TEXT NOT NULL, title_score REAL, artist_score REAL,
  PRIMARY KEY(work_id, ht_id));
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ------------------------------------------------------------------ works
@dataclass
class Work:
    work_id: str
    title: str
    artist: str
    artists: list[str]          # canonical first, then original and every search artist
    year: int | None = None
    rank: int | None = None

    @cached_property
    def title_forms(self) -> list[str]:
        """Squashed title keys, longest first ("icantgetnosatisfaction", "satisfaction")."""
        forms = {squash(title_key(self.title)), squash(title_core(self.title))}
        return sorted((f for f in forms if f), key=len, reverse=True)

    @cached_property
    def artist_forms(self) -> list[str]:
        forms: set[str] = set()
        for a in self.artists:
            for f in (artist_key(a), primary_artist(a)):
                s = squash(f)
                if len(s) >= 2:
                    forms.add(s)
        return sorted(forms, key=len, reverse=True)


def load_works(conn: sqlite3.Connection, ids: list[str] | None = None, limit: int | None = None,
               pool_only: bool = True) -> list[Work]:
    sql = ("SELECT work_id, title, canonical_artist, original_artist, search_artists, work_year, canon_rank"
           " FROM work")
    cond, params = [], []
    if ids:
        cond.append(f"work_id IN ({','.join('?' * len(ids))})")
        params += ids
    elif pool_only:
        cond.append("in_pool = 1")
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY canon_rank IS NULL, canon_rank, work_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    works = []
    for r in conn.execute(sql, params):
        artists = [r["canonical_artist"]]
        if r["original_artist"]:
            artists.append(r["original_artist"])
        try:
            artists += [a for a in json.loads(r["search_artists"] or "[]") if isinstance(a, str)]
        except json.JSONDecodeError:
            pass
        seen: list[str] = []
        for a in artists:
            if a and a.strip() and a not in seen:
                seen.append(a)
        works.append(Work(r["work_id"], r["title"], r["canonical_artist"], seen, r["work_year"], r["canon_rank"]))
    return works


# ------------------------------------------------------------------ name matching
@dataclass
class Match:
    title_score: float
    artist_score: float
    accepted: bool
    reason: str                 # 'accept' | 'title_only' | 'other_artist' | 'no_title'


_EXT = re.compile(r"\.(?:mid|midi|kar|rmi|smf)$", re.I)
_DUP = re.compile(r"(?:\.\d+|\s*\(\d+\))$")
_TAG = re.compile(r"(?:[\s_\-]+(?:k|l|kar|karaoke|gm|gs|xg|midi|mid))+$", re.I)
_SEP = re.compile(r"\s*[-–—~]{1,3}\s+|\s+[-–—~]{1,3}\s*|--+")
# Filler that may surround the title in a squashed name: counters, karaoke/sequencer tags.
# Matched token by token with a small dynamic programme: a regex like (a|b|\d+)* backtracks
# exponentially on long runs that almost match.
_NOISE_TOKEN = re.compile(r"\d+|v\d*|ver\d*|version\d*|kar|karaoke|k|l|gm|gs|xg|midi|mid|remix|mix|live|demo"
                          r"|the|by|[a-z]\d+|\d+[a-z]{1,3}\d*|[a-z]{2}\d+")
_FEAT_WORD = re.compile(r"feat|featuring|ft")
_MAX_REST = 60


def _is_noise(s: str) -> bool:
    n = len(s)
    if n > _MAX_REST:
        return False
    ok = [True] + [False] * n
    for i in range(n):
        if ok[i]:
            for j in range(i + 1, n + 1):
                if not ok[j] and _NOISE_TOKEN.fullmatch(s, i, j):
                    ok[j] = True
    return ok[n]


_GENERIC = {
    "midi", "midis", "mid", "mids", "music", "songs", "song", "various", "variousartists", "misc", "unknown",
    "artists", "artistsbands", "pop", "rock", "oldies", "hits", "karaoke", "kar", "new", "files", "lmdfull",
    "cleanmidi", "nationalanthems", "tvthemes", "moviethemes", "videogames", "seasonal", "download",
}


def split_name(name: str) -> tuple[list[str], str]:
    parts = [p for p in name.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        return [], ""
    base = _EXT.sub("", parts[-1])
    base = _DUP.sub("", base)
    base = base.replace("_", " ").strip()
    base = _TAG.sub("", base).strip() or base
    return parts[:-1], base


def _specific(artist_text: str | None) -> bool:
    if not artist_text:
        return False
    s = squash(fold(artist_text))
    return len(s) >= 2 and not s.isdigit() and s not in _GENERIC


def artist_score(text: str, artist: str) -> float:
    """``textnorm.artist_matches`` without its substring shortcut inside longer words.

    textnorm scores "Queensryche" 95 for "Queen" (every artist token is contained in the
    squashed name). Here a token must be a whole word unless the squashed forms are equal.
    """
    s = artist_matches(text, artist)
    if ARTIST_MIN <= s < 100.0:
        words = set(fold(text).split())
        if not all(t in words or squash(t) in words for t in artist_tokens(artist)):
            s = min(s, float(fuzz.token_set_ratio(artist_key(artist), artist_key(text))))
    return s


def _explained(rest: str, sa: str) -> bool:
    """``rest`` is filler, or the artist ``sa`` surrounded by filler, optionally followed by a
    featuring credit ("featbrunomars")."""
    if not rest:
        return not sa
    if len(rest) > _MAX_REST:
        return False
    cores = [rest] + [rest[:m.start()] for m in _FEAT_WORD.finditer(rest)]
    for core in cores:
        if not sa:
            if _is_noise(core):
                return True
            continue
        i = core.find(sa)
        while i >= 0:
            if _is_noise(core[:i]) and _is_noise(core[i + len(sa):]):
                return True
            i = core.find(sa, i + 1)
    return False


def readings(name: str, artist_hint: str | None = None) -> list[tuple[str | None, str]]:
    """(artist text or None, title text) interpretations of a file name or listing entry."""
    dirs, base = split_name(name)
    out: list[tuple[str | None, str]] = []
    if artist_hint:
        out.append((artist_hint, base))
    parts = [p.strip() for p in _SEP.split(base) if p and p.strip()]
    if len(parts) >= 2:
        out += [(parts[0], parts[-1]), (parts[-1], parts[0])]
        if len(parts) > 2:
            out += [(parts[0], " ".join(parts[1:])), (" ".join(parts[:-1]), parts[-1]), (parts[1], parts[-1])]
    for d in reversed(dirs[-2:]):
        out.append((d, base))
        if len(parts) >= 2:
            out.append((d, parts[-1]))
    out.append((None, base))
    return out


def match_name(name: str, work: Work, artist_hint: str | None = None) -> Match:
    """Best reading of ``name`` for ``work`` (see module docstring)."""
    best: tuple[bool, float, float] = (False, 0.0, 0.0)
    other_artist = False
    dirs, base = split_name(name)

    def consider(ts: float, ars: float) -> None:
        nonlocal best
        cand = (ts >= TITLE_MIN and ars >= ARTIST_MIN, ts, ars)
        if (cand[0], cand[1] + cand[2]) > (best[0], best[1] + best[2]):
            best = cand

    for artist_text, title_text in readings(name, artist_hint):
        ts = title_matches(title_text, work.title) if title_text else 0.0
        if ts < 60:
            continue
        ars = 0.0
        if _specific(artist_text):
            ars = max(artist_score(artist_text, a) for a in work.artists)
            if ts >= TITLE_MIN and ars < ARTIST_MIN:
                other_artist = True
        consider(ts, ars)

    # Squashed containment: "queenbohemianrhapsody2", "withorwithoutyou2", slugs.
    b = squash(fold(base))
    hint_score = 0.0
    if artist_hint and _specific(artist_hint):
        hint_score = max(artist_score(artist_hint, a) for a in work.artists)
    dir_score = max((artist_score(d, a) for d in dirs[-2:] if _specific(d) for a in work.artists), default=0.0)
    for t in work.title_forms:
        if len(t) < 4 and t != b:
            continue
        i = b.find(t)
        if i < 0:
            continue
        pre, post = b[:i], b[i + len(t):]
        if _explained(pre, "") and _explained(post, ""):
            consider(100.0, max(hint_score, dir_score))
            continue
        for sa in work.artist_forms:
            if (_explained(pre, sa) and _explained(post, "")) or (_explained(pre, "") and _explained(post, sa)):
                consider(100.0, 100.0)
                break
        else:
            consider(100.0 * len(t) / max(len(b), 1), 0.0)

    accepted, ts, ars = best
    if accepted:
        reason = "accept"
    elif ts >= TITLE_MIN:
        reason = "other_artist" if other_artist else "title_only"
    else:
        reason = "no_title"
    return Match(round(ts, 1), round(ars, 1), accepted, reason)


# ------------------------------------------------------------------ candidates
@dataclass
class Hit:
    """A file a source believes is a recording of the work."""

    source: str
    source_ref: str             # md5 (Lakh), song id (web sources)
    orig_name: str              # the source's own path / filename / listing title
    title_score: float
    artist_score: float
    match_class: str            # 'accept' | 'probable'
    url: str | None = None
    lmd_match_score: float | None = None
    lmd_msd_id: str | None = None
    extra: dict = field(default_factory=dict)

    def sort_key(self) -> tuple:
        return (self.match_class != "accept", -(self.lmd_match_score or 0.0),
                -(self.title_score + self.artist_score), self.source_ref)


def candidate_file(work_id: str, source: str, md5: str) -> Path:
    return config.CANDIDATES / work_id / f"{source}__{md5}.mid"


def stored_path(p: Path) -> str:
    """Path as stored in the DB: relative to the repo root when possible ('data/...')."""
    try:
        return p.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def resolve(stored: str) -> Path:
    return config.ROOT / stored  # absolute stored paths stay absolute


def valid_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT work_id, COUNT(*) FROM candidate WHERE valid = 1 GROUP BY work_id")}


def attempted(conn: sqlite3.Connection, work_id: str, source: str, ref: str) -> str | None:
    """Status of an earlier attempt, or None when the file should be (re)tried.

    Network errors (detail 'net:...') are retried on the next run; everything else is final.
    """
    row = conn.execute("SELECT status, detail FROM fetch_attempt WHERE work_id=? AND source=? AND source_ref=?",
                       (work_id, source, ref)).fetchone()
    if row is None or (row[0] == "download_failed" and (row[1] or "").startswith("net:")):
        return None
    return row[0]


def record_attempt(conn: sqlite3.Connection, work_id: str, source: str, ref: str, status: str,
                   md5: str | None = None, nbytes: int | None = None, detail: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fetch_attempt(work_id, source, source_ref, status, md5, bytes, detail, attempted_at)"
        " VALUES (?,?,?,?,?,?,?,datetime('now'))", (work_id, source, ref, status, md5, nbytes, detail))


def mark_searched(conn: sqlite3.Connection, work_id: str, source: str, n_matches: int, n_rejected: int,
                  error: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fetch_status(work_id, source, searched_at, n_matches, n_rejected, error)"
        " VALUES (?,?,datetime('now'),?,?,?)", (work_id, source, n_matches, n_rejected, error))


def searched(conn: sqlite3.Connection, source: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT work_id FROM fetch_status WHERE source=? AND error IS NULL", (source,))}


def ingest(conn: sqlite3.Connection, work: Work, hit: Hit, data: bytes, *, replace: bool = False,
           processed: midi.Processed | None = None) -> str:
    """Validate/sanitize ``data`` and store it as a candidate. Returns the attempt status.

    ``processed`` is ``midi.process(data)`` when the caller already ran it (in a worker pool).
    """
    md5 = hashlib.md5(data).hexdigest()
    hit.extra["md5"] = md5
    row = conn.execute("SELECT candidate_id, source FROM candidate WHERE work_id=? AND md5=?",
                       (work.work_id, md5)).fetchone()
    if row and not (replace and row["source"] == hit.source):
        record_attempt(conn, work.work_id, hit.source, hit.source_ref, "duplicate", md5, len(data), row["source"])
        return "duplicate"
    proc = processed if processed is not None else midi.process(data)
    path_s, sha = None, None
    if proc.ok and proc.data is not None:
        path = candidate_file(work.work_id, hit.source, md5)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(proc.data)
        tmp.replace(path)
        path_s, sha = stored_path(path), hashlib.sha256(proc.data).hexdigest()
    feats = json.dumps(proc.features, separators=(",", ":")) if proc.features else None
    values = (work.work_id, hit.source, hit.source_ref, hit.url, hit.orig_name[:300], md5, sha, len(data),
              hit.title_score, hit.artist_score, hit.match_class, hit.lmd_match_score, hit.lmd_msd_id,
              path_s, int(proc.ok), proc.reason, feats)
    if row:
        conn.execute("DELETE FROM candidate WHERE candidate_id=?", (row["candidate_id"],))
    conn.execute(
        "INSERT INTO candidate(work_id, source, source_ref, url, orig_name, md5, sha256, bytes, title_score,"
        " artist_score, match_class, lmd_match_score, lmd_msd_id, sanitized_path, valid, invalid_reason,"
        " features_json, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))", values)
    status = "new" if proc.ok else "invalid"
    record_attempt(conn, work.work_id, hit.source, hit.source_ref, status, md5, len(data), proc.reason)
    return status


@dataclass
class SourceStats:
    source: str
    works: int = 0              # works searched
    matched_works: int = 0      # works with at least one accepted/probable hit
    hits: int = 0               # accepted/probable files (after the per-work cap)
    new: int = 0
    valid: int = 0
    duplicate: int = 0
    invalid: int = 0
    failed: int = 0             # download failures
    rejected: int = 0           # title matches rejected (title-only, other artist)
    seconds: float = 0.0
    requests: int = 0
    invalid_reasons: dict[str, int] = field(default_factory=dict)
    _t0: float = field(default_factory=time.monotonic, repr=False)

    def count(self, status: str, conn: sqlite3.Connection | None = None, work_id: str | None = None,
              md5: str | None = None) -> None:
        if status == "new":
            self.new += 1
            self.valid += 1
        elif status == "invalid":
            self.invalid += 1
            if conn is not None and work_id and md5:
                r = conn.execute("SELECT invalid_reason FROM candidate WHERE work_id=? AND md5=?",
                                 (work_id, md5)).fetchone()
                if r:
                    self.invalid_reasons[r[0]] = self.invalid_reasons.get(r[0], 0) + 1
        elif status == "duplicate":
            self.duplicate += 1
        elif status == "download_failed":
            self.failed += 1

    def done(self) -> "SourceStats":
        self.seconds = round(time.monotonic() - self._t0, 1)
        return self

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
