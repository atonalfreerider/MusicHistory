"""Transient lyric reader for the themes stage (DESIGN.md §12).

**Lyric text never leaves process memory.** This module re-reads a song's own MIDI
candidates, decodes their lyric events into lines and hands those lines to the classifier.
Nothing here writes text anywhere: no files, no database rows, no log lines, no exception
messages (errors are reported by type and candidate id only). ``Lyrics.__repr__`` hides the
text so a stray print or traceback cannot leak it.

Which candidates: every candidate of a selected work (``work.selected >= 1``) whose
features show lyrics, i.e. ``lyric_events >= 20`` (lyric meta events) or a Soft-Karaoke file
(``karaoke`` with an ``@K`` tag) with ``text_events >= 20`` (KAR syllables are text events).
Best first: the chosen candidate, then valid ones, then the most lyric events. The first
candidate that decodes into real text (``MIN_WORDS`` words) wins; the next one is tried when
a file decodes into junk.

Where the bytes come from (raw files are never stored by the fetch stage):

* ``lakh`` / ``lakh_clean``: streamed out of the cached tarballs in ``data/cache/lakh`` with
  ``LakhIndex.extract`` (one pass over ``lmd_full.tar.gz``, then ``clean_midi.tar.gz`` for
  the rest), raw bytes kept only until they are decoded;
* ``freemidi`` / ``midicollection`` / ``midiworld``: downloaded again into memory through
  the adapters' own polite download paths (``PoliteClient``: robots.txt, per-host intervals,
  no disk cache for the MIDI bytes or song pages, no ``fetch_log`` rows). ``web=False``
  skips them; they are counted.

Decoding: lyric meta events (``FF 05``) of the main lyric track (plus other lyric tracks that
are not duplicates of it, e.g. a duet's second voice), or the non-``@`` text events of the
``@``-tagged Soft-Karaoke words track. Bytes decode as UTF-8 when every event does, else
cp1252 (latin-1 for its five undefined bytes). ``/`` and ``\\`` (KAR line/paragraph marks) and
CR/LF end a line; syllables join into words (``Hel-`` + ``lo`` -> ``Hello``; files without any
spacing are read one word per event). Credit/URL lines are dropped.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..midi import validate
from ..midi.validate import META_LYRIC, META_TEXT

MIN_EVENTS = 20          # lyric (or KAR text) events a candidate needs to be read at all
MIN_WORDS = 20           # decoded text with fewer words is not treated as lyrics
MAX_LINE_WORDS = 16      # longer "lines" (files without line marks) are re-split
RESPLIT_WORDS = 8
LAKH_SOURCES = ("lakh", "lakh_clean")
WEB_SOURCES = ("freemidi", "midicollection", "midiworld")
DECODER_VERSION = "1"


# ------------------------------------------------------------------ candidates
@dataclass(frozen=True)
class LyricCandidate:
    candidate_id: int
    work_id: str
    source: str
    md5: str
    source_ref: str | None
    url: str | None
    chosen: bool
    valid: bool
    kind: str            # 'lyric' (lyric meta events) | 'kar' (Soft-Karaoke text events)
    n_events: int

    @property
    def is_web(self) -> bool:
        return self.source in WEB_SOURCES


def eligible(features: dict) -> tuple[str, int] | None:
    """('lyric' | 'kar', event count) when a candidate's features show lyrics, else None."""
    le = int(features.get("lyric_events") or 0)
    if le >= MIN_EVENTS:
        return "lyric", le
    te = int(features.get("text_events") or 0)
    if features.get("karaoke") and te >= MIN_EVENTS:
        return "kar", te
    return None


def plan(conn: sqlite3.Connection, work_ids: Iterable[str] | None = None) -> dict[str, list[LyricCandidate]]:
    """Lyric-bearing candidates per selected work, best first (chosen, valid, most events)."""
    sql = ("SELECT c.candidate_id, c.work_id, c.source, c.md5, c.source_ref, c.url, c.chosen, c.valid,"
           " c.features_json FROM candidate c JOIN work w USING(work_id)"
           " WHERE w.selected >= 1 AND c.features_json IS NOT NULL")
    keep = set(work_ids) if work_ids is not None else None
    out: dict[str, list[LyricCandidate]] = {}
    for r in conn.execute(sql):
        if keep is not None and r[1] not in keep:
            continue
        try:
            feats = json.loads(r[8])
        except (TypeError, json.JSONDecodeError):
            continue
        e = eligible(feats)
        if e is None:
            continue
        out.setdefault(r[1], []).append(LyricCandidate(
            candidate_id=int(r[0]), work_id=r[1], source=r[2], md5=r[3], source_ref=r[4], url=r[5],
            chosen=bool(r[6]), valid=bool(r[7]), kind=e[0], n_events=e[1]))
    for lst in out.values():
        lst.sort(key=lambda c: (not c.chosen, not c.valid, -c.n_events, c.candidate_id))
    return out


# ------------------------------------------------------------------ decoding
@dataclass
class Lyrics:
    """Decoded lyric lines of one song. Memory only; the repr never shows the text."""

    lines: list[str] = field(repr=False)
    candidate_id: int
    source: str
    kind: str

    def __repr__(self) -> str:  # never the text
        return f"Lyrics(<{len(self.lines)} lines>, candidate={self.candidate_id}, source={self.source})"

    @property
    def n_lines(self) -> int:
        return len(self.lines)

    def sha256(self) -> str:
        return text_sha256(self.lines)


def text_sha256(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _cp1252(b: bytes) -> str:
    try:
        return b.decode("cp1252")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def _decoder(chunks: list[bytes]) -> Callable[[bytes], str]:
    try:
        for b in chunks:
            b.decode("utf-8")
    except UnicodeDecodeError:
        return _cp1252
    return lambda b: b.decode("utf-8")


_BREAK = re.compile(r"[\r\n/\\]+")
_JUNK_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f^~<>{}\[\]|*#=]")
_CREDIT = re.compile(r"(?:copyright|\(c\)|©|www\.|https?:|\.com\b|\.net\b|\.org\b|karaoke|sequenced|"
                     r"midi\b|words and music|lyrics by|music by|written by|transcribed|e-?mail)", re.I)
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")


def _clean_line(s: str) -> str:
    s = _JUNK_CHARS.sub(" ", s.replace("_", " "))
    return re.sub(r"\s+", " ", s).strip()


def syllables_to_lines(syllables: list[str]) -> list[str]:
    """Join lyric syllables into lines (see module docstring)."""
    parts: list[list[str]] = []   # per event: pieces separated by line breaks
    for s in syllables:
        parts.append(_BREAK.split(s))
    bodies = [p for pieces in parts for p in pieces if p]
    spaced_n = sum(1 for p in bodies if p[:1].isspace() or p[-1:].isspace())
    spaced = bool(bodies) and spaced_n / len(bodies) >= 0.15
    lines: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        line = _clean_line("".join(cur))
        cur.clear()
        if line:
            lines.append(line)

    for pieces in parts:
        for i, p in enumerate(pieces):
            if i > 0:
                flush()
            if not p:
                continue
            if spaced:
                if cur and cur[-1].endswith("-") and not p[:1].isspace() and cur[-1].strip() != "-":
                    cur[-1] = cur[-1][:-1]          # hyphenated syllable: "Hel-" + "lo"
                cur.append(p)
            else:
                p = p.strip()
                if not p:
                    continue
                if cur:
                    if cur[-1].endswith("-"):
                        cur[-1] = cur[-1][:-1]
                    elif p.startswith("-"):
                        p = p[1:]
                    else:
                        cur.append(" ")
                cur.append(p)
    flush()
    return lines


def _resplit(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        words = line.split()
        if len(words) <= MAX_LINE_WORDS:
            out.append(line)
            continue
        for i in range(0, len(words), RESPLIT_WORDS):
            out.append(" ".join(words[i:i + RESPLIT_WORDS]))
    return out


def n_words(lines: list[str]) -> int:
    return sum(len(_WORD.findall(line)) for line in lines)


def _pick_tracks(tracks: list[list[tuple[int, bytes]]]) -> list[tuple[int, bytes]]:
    """The main lyric track plus other lyric tracks that are not copies of it (duet parts)."""
    tracks = sorted((t for t in tracks if t), key=len, reverse=True)
    if not tracks:
        return []
    main = list(tracks[0])
    main_ticks = {t for t, _ in main}
    for other in tracks[1:]:
        if len(other) < MIN_EVENTS // 2:
            continue
        shared = sum(1 for t, _ in other if t in main_ticks)
        if shared / len(other) < 0.5:
            main += other
            main_ticks |= {t for t, _ in other}
    main.sort(key=lambda e: e[0])  # stable: same-tick events keep their track order
    return main


def decode_midi(data: bytes) -> tuple[str, list[str]] | None:
    """('lyric' | 'kar', lines) from raw MIDI bytes, or None when no usable lyrics.

    Never raises for a hostile file (returns None); never includes text in an error.
    """
    try:
        raw = validate.parse(data)
    except Exception:
        return None
    lyric_tracks: list[list[tuple[int, bytes]]] = []
    kar_tracks: list[list[tuple[int, bytes]]] = []
    plain_text: list[list[tuple[int, bytes]]] = []
    has_k = False
    for tr in raw.tracks:
        lyr = [(e.tick, e.data) for e in tr if e.status == 0xFF and e.meta == META_LYRIC]
        texts = [(e.tick, e.data) for e in tr if e.status == 0xFF and e.meta == META_TEXT]
        if lyr:
            lyric_tracks.append(lyr)
        if texts:
            has_k = has_k or any(d[:2].upper() == b"@K" for _, d in texts)
            words = [(t, d) for t, d in texts if d[:1] != b"@"]
            if any(d[:1] == b"@" for _, d in texts):
                kar_tracks.append(words)
            plain_text.append(words)
    candidates: list[tuple[str, list[tuple[int, bytes]]]] = []
    if sum(len(t) for t in lyric_tracks) >= MIN_EVENTS:
        candidates.append(("lyric", _pick_tracks(lyric_tracks)))
    if has_k:
        words = max(kar_tracks, key=len, default=[])
        if len(words) < MIN_EVENTS:  # words track without its own '@' tags
            words = max(plain_text, key=len, default=[])
        if len(words) >= MIN_EVENTS:
            candidates.append(("kar", sorted(words, key=lambda e: e[0])))
    best: tuple[int, str, list[str]] | None = None
    for kind, events in candidates:
        chunks = [d for _, d in events]
        dec = _decoder(chunks)
        lines = [ln for ln in syllables_to_lines([dec(b) for b in chunks]) if not _CREDIT.search(ln)]
        lines = [ln for ln in _resplit(lines) if _WORD.search(ln)]
        nw = n_words(lines)
        if nw >= MIN_WORDS and (best is None or nw > best[0]):
            best = (nw, kind, lines)
    return (best[1], best[2]) if best else None


# ------------------------------------------------------------------ reading
@dataclass
class ReadStats:
    works_planned: int = 0            # works with at least one lyric-bearing candidate
    works_with_lyrics: int = 0
    lakh_wanted: int = 0              # Lakh candidates streamed for
    lakh_found: int = 0
    lakh_seconds: float = 0.0
    web_needed: int = 0               # web candidates a work reached in its preference order
    web_downloaded: int = 0
    web_failed: int = 0
    web_skipped: int = 0              # web=False
    web_changed: int = 0              # the site now serves different bytes (used anyway)
    web_requests_seconds: float = 0.0
    decode_failed: int = 0            # candidates whose bytes gave no usable text
    by_source: dict[str, int] = field(default_factory=dict)   # winning candidate source -> works
    by_kind: dict[str, int] = field(default_factory=dict)     # 'lyric' | 'kar' -> works
    fallback_used: int = 0            # works whose first-choice candidate did not decode

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _lakh_bytes(md5s: set[str]) -> Iterable[tuple[str, bytes]]:
    """(md5, raw bytes) for the wanted Lakh files: one streaming pass per tarball, memory only."""
    from ..sources import lakh

    idx = lakh.LakhIndex(lakh.lakh_dir() / "index.sqlite")
    try:
        yield from idx.extract(md5s)
    finally:
        idx.conn.close()


def _web_client():
    from .. import config
    from ..http import PoliteClient
    from ..sources import freemidi, midicollection, midiworld

    # Same per-host politeness as the fetch stage. cache_dir only serves freemidi's cached
    # search pages (listings); MIDI bytes and song pages are fetched with use_cache=False.
    # db=None: no fetch_log rows (that table belongs to fetch).
    return PoliteClient(
        min_interval={"midicollection.com": midicollection.INTERVAL, "freemidi.org": freemidi.INTERVAL,
                      "www.midiworld.com": midiworld.INTERVAL},
        default_interval=3.0, timeout=30.0, cache_dir=config.CACHE / "http", db=None)


def _web_bytes(conn: sqlite3.Connection, client, cand: LyricCandidate) -> bytes | None:
    """Re-download one web candidate into memory via its adapter's download path."""
    from ..sources import base, freemidi

    st = base.SourceStats(cand.source)
    if cand.source == "freemidi":
        works = base.load_works(conn, ids=[cand.work_id], pool_only=False)
        if not works or not cand.source_ref:
            return None
        page = freemidi._page_url(conn, client, cand.source_ref, works[0], st)
        if not page:
            return None
        data, _ = freemidi.download(client, page, cand.source_ref, st)
        return data
    if not cand.url:
        return None
    r = client.get(cand.url, use_cache=False)
    return r.content if r.status == 200 and r.content else None


def read(conn: sqlite3.Connection, planned: dict[str, list[LyricCandidate]], *, web: bool = True,
         progress: Callable[[str], None] = lambda s: print(s, flush=True)) -> tuple[dict[str, Lyrics], ReadStats]:
    """Decode the best lyric candidate of every planned work. Returns work_id -> Lyrics (memory only)."""
    stats = ReadStats(works_planned=len(planned))
    decoded: dict[int, tuple[str, list[str]] | None] = {}   # candidate_id -> decode result

    # 1. Lakh: one streaming pass over the tarballs for every Lakh candidate of every work.
    by_md5: dict[str, list[LyricCandidate]] = {}
    for lst in planned.values():
        for c in lst:
            if c.source in LAKH_SOURCES:
                by_md5.setdefault(c.md5, []).append(c)
    stats.lakh_wanted = len(by_md5)
    if by_md5:
        t0 = time.monotonic()
        progress(f"themes: streaming Lakh tarballs for {len(by_md5)} lyric files")
        for md5, data in _lakh_bytes(set(by_md5)):
            if hashlib.md5(data).hexdigest() != md5:
                continue
            stats.lakh_found += 1
            res = decode_midi(data)
            del data
            stats.decode_failed += res is None
            for c in by_md5.get(md5, ()):
                decoded[c.candidate_id] = res
        stats.lakh_seconds = round(time.monotonic() - t0, 1)
        progress(f"themes: Lakh {stats.lakh_found}/{stats.lakh_wanted} files in {stats.lakh_seconds:.0f} s")

    # 2. Per work, walk the preference order; web candidates are downloaded only when reached.
    client = None
    out: dict[str, Lyrics] = {}
    for work_id, lst in planned.items():
        for rank, c in enumerate(lst):
            if c.candidate_id not in decoded and c.is_web:
                stats.web_needed += 1
                if not web:
                    stats.web_skipped += 1
                    decoded[c.candidate_id] = None
                    continue
                client = client or _web_client()
                t0 = time.monotonic()
                try:
                    data = _web_bytes(conn, client, c)
                except Exception as exc:  # network trouble; never include response text
                    progress(f"themes: web candidate {c.candidate_id} ({c.source}) failed: {type(exc).__name__}")
                    data = None
                stats.web_requests_seconds += time.monotonic() - t0
                if data is None:
                    stats.web_failed += 1
                    decoded[c.candidate_id] = None
                    continue
                stats.web_downloaded += 1
                if hashlib.md5(data).hexdigest() != c.md5:
                    stats.web_changed += 1
                decoded[c.candidate_id] = decode_midi(data)
                del data
                stats.decode_failed += decoded[c.candidate_id] is None
            res = decoded.get(c.candidate_id)
            if res is None:
                continue
            kind, lines = res
            out[work_id] = Lyrics(lines, c.candidate_id, c.source, kind)
            stats.by_source[c.source] = stats.by_source.get(c.source, 0) + 1
            stats.by_kind[kind] = stats.by_kind.get(kind, 0) + 1
            stats.fallback_used += int(rank > 0)
            break
    stats.works_with_lyrics = len(out)
    stats.web_requests_seconds = round(stats.web_requests_seconds, 1)
    decoded.clear()
    return out, stats
