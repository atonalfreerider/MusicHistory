"""Lakh MIDI Dataset adapter: offline index + streaming extraction (DESIGN.md §5).

The LMD files are named by MD5 only; their artist/title lives in the *original* scraped
paths (``md5_to_paths.json``, 570,601 paths for 178,561 files), in ``clean_midi``'s
``Artist/Title.mid`` layout, and, for the 45k audio-matched files, in the Million Song
Dataset track the file was matched to (``match_scores.json`` + ``unique_tracks.txt``).
All of it goes into one SQLite file (``data/cache/lakh/index.sqlite``) with an FTS5
trigram index over the squashed paths, so matching a work is a substring query plus
scoring of a few hundred paths, not a scan of half a million.

Files are extracted by streaming the tarballs once per batch (``tarfile`` stream mode,
~35 s for lmd_full) and picking only the wanted members; nothing else is unpacked, and the
raw members are never written to disk (only their sanitized versions are).

Source labels: ``lakh_clean`` when a ``clean_midi`` path of the file is accepted (better
labelled), else ``lakh``. The file itself is taken from lmd_full when present there.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tarfile
import time
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .. import config, midi
from ..http import download
from ..textnorm import fold, squash, title_matches
from . import base
from .base import ARTIST_MIN, TITLE_MIN, Hit, SourceStats, Work

LMD = "http://hog.ee.columbia.edu/craffel/lmd/"  # HTTP only: the host's TLS is broken
FILES: dict[str, tuple[str, str | None]] = {
    "lmd_full.tar.gz": (LMD + "lmd_full.tar.gz", "2536ce3fd2cede53ddaa264f731859ab"),
    "clean_midi.tar.gz": (LMD + "clean_midi.tar.gz", None),
    "md5_to_paths.json": (LMD + "md5_to_paths.json", None),
    "match_scores.json": (LMD + "match_scores.json", None),
    "unique_tracks.txt": ("http://millionsongdataset.com/sites/default/files/AdditionalFiles/unique_tracks.txt", None),
}
INDEX_VERSION = "1"
SOURCES = ("lakh", "lakh_clean")
BATCH = 256  # extracted files sanitized per worker-pool round

INDEX_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE path(id INTEGER PRIMARY KEY, md5 TEXT NOT NULL, path TEXT NOT NULL,
                  src TEXT NOT NULL,          -- 'full' (md5_to_paths) | 'clean' (clean_midi)
                  sq TEXT NOT NULL);          -- squashed folded path, for substring search
CREATE INDEX path_md5 ON path(md5);
CREATE VIRTUAL TABLE path_fts USING fts5(sq, content='path', content_rowid='id', tokenize='trigram');
CREATE TABLE clean_member(name TEXT PRIMARY KEY, md5 TEXT NOT NULL, bytes INTEGER NOT NULL);
CREATE INDEX clean_md5 ON clean_member(md5);
CREATE TABLE full_md5(md5 TEXT PRIMARY KEY);
CREATE TABLE lmd_match(md5 TEXT NOT NULL, msd_id TEXT NOT NULL, score REAL NOT NULL, PRIMARY KEY(md5, msd_id));
CREATE TABLE msd(msd_id TEXT PRIMARY KEY, artist TEXT NOT NULL, title TEXT NOT NULL);
"""


def lakh_dir() -> Path:
    return config.CACHE / "lakh"


def ensure_files(names: tuple[str, ...] = tuple(FILES), offline: bool = False) -> dict[str, Path]:
    """Download (resumably, MD5-checked for lmd_full) whatever is missing from the cache."""
    out = {}
    for name in names:
        dest = lakh_dir() / name
        if not dest.exists():
            if offline:
                raise FileNotFoundError(f"{dest} is missing and --offline was given")
            url, md5 = FILES[name]
            print(f"lakh: downloading {url}", flush=True)
            download(url, dest, expected_md5=md5)
        out[name] = dest
    return out


def _sq(path: str) -> str:
    return squash(fold(path.rsplit(".", 1)[0] if path.lower().endswith((".mid", ".midi", ".kar")) else path))


def build_index(force: bool = False, offline: bool = False) -> Path:
    dest = lakh_dir() / "index.sqlite"
    if dest.exists() and not force:
        with sqlite3.connect(dest) as c:
            try:
                if c.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0] == INDEX_VERSION:
                    return dest
            except (sqlite3.Error, TypeError):
                pass
    files = ensure_files(offline=offline)
    t0 = time.monotonic()
    tmp = dest.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    c = sqlite3.connect(tmp)
    c.executescript(INDEX_SCHEMA)
    md5_to_paths: dict[str, list[str]] = json.loads(files["md5_to_paths.json"].read_text(encoding="utf-8"))
    rows = ((md5, p, "full", _sq(p)) for md5, paths in md5_to_paths.items() for p in paths)
    c.executemany("INSERT INTO path(md5, path, src, sq) VALUES (?,?,?,?)", rows)
    c.executemany("INSERT INTO full_md5(md5) VALUES (?)", ((m,) for m in md5_to_paths))
    del md5_to_paths
    clean_rows = []
    with tarfile.open(files["clean_midi.tar.gz"], "r|gz") as tf:
        for ti in tf:
            if not ti.isfile():
                continue
            f = tf.extractfile(ti)
            if f is None:
                continue
            clean_rows.append((ti.name, hashlib.md5(f.read()).hexdigest(), ti.size))
    c.executemany("INSERT INTO clean_member(name, md5, bytes) VALUES (?,?,?)", clean_rows)
    c.executemany("INSERT INTO path(md5, path, src, sq) VALUES (?,?,?,?)",
                  ((md5, name.split("/", 1)[1], "clean", _sq(name.split("/", 1)[1])) for name, md5, _ in clean_rows))
    scores: dict[str, dict[str, float]] = json.loads(files["match_scores.json"].read_text(encoding="utf-8"))
    c.executemany("INSERT INTO lmd_match(md5, msd_id, score) VALUES (?,?,?)",
                  ((md5, msd, float(s)) for msd, d in scores.items() for md5, s in d.items()))
    wanted = set(scores)
    msd_rows = []
    with open(files["unique_tracks.txt"], encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("<SEP>")
            if len(parts) == 4 and parts[0] in wanted:
                msd_rows.append((parts[0], parts[2], parts[3]))
    c.executemany("INSERT OR IGNORE INTO msd(msd_id, artist, title) VALUES (?,?,?)", msd_rows)
    c.execute("INSERT INTO path_fts(path_fts) VALUES ('rebuild')")
    c.executemany("INSERT INTO meta(key, value) VALUES (?,?)", [
        ("version", INDEX_VERSION), ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
        ("n_paths", str(c.execute("SELECT COUNT(*) FROM path").fetchone()[0])),
        ("n_clean", str(len(clean_rows))), ("n_msd", str(len(msd_rows))),
        ("build_seconds", f"{time.monotonic() - t0:.1f}"),
    ])
    c.commit()
    c.close()
    tmp.replace(dest)
    print(f"lakh: index built in {time.monotonic() - t0:.0f} s -> {dest}", flush=True)
    return dest


class LakhIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)

    @classmethod
    def open(cls, *, rebuild: bool = False, offline: bool = False) -> "LakhIndex":
        return cls(build_index(force=rebuild, offline=offline))

    def meta(self) -> dict[str, str]:
        return dict(self.conn.execute("SELECT key, value FROM meta"))

    def _rows(self, work: Work) -> dict[int, tuple[str, str, str]]:
        rows: dict[int, tuple[str, str, str]] = {}
        queries = [t for t in work.title_forms if len(t) >= 3]
        if not queries:  # very short titles: find the artist, the scorer checks the title
            queries = [a for a in work.artist_forms if len(a) >= 3]
        for q in queries:
            for rid, md5, path, src in self.conn.execute(
                "SELECT p.id, p.md5, p.path, p.src FROM path_fts JOIN path p ON p.id = path_fts.rowid"
                " WHERE path_fts MATCH ?", (f'"{q}"',)):
                rows[rid] = (md5, path, src)
        return rows

    def _msd_matches(self, work: Work, md5s: list[str]) -> dict[str, tuple[float, str, float]]:
        """md5 -> (best match score, msd id, artist score) over MSD tracks naming the work."""
        out: dict[str, tuple[float, str, float]] = {}
        for i in range(0, len(md5s), 500):
            chunk = md5s[i:i + 500]
            for md5, msd_id, score, artist, title in self.conn.execute(
                "SELECT m.md5, m.msd_id, m.score, s.artist, s.title FROM lmd_match m JOIN msd s USING(msd_id)"
                f" WHERE m.md5 IN ({','.join('?' * len(chunk))})", chunk):
                if title_matches(title, work.title) < TITLE_MIN:
                    continue
                ars = max(base.artist_score(artist, a) for a in work.artists)
                if ars < ARTIST_MIN:
                    continue
                if md5 not in out or score > out[md5][0]:
                    out[md5] = (score, msd_id, ars)
        return out

    def search(self, work: Work, max_per_source: int = base.MAX_PER_SOURCE) -> tuple[dict[str, list[Hit]], int]:
        """Accepted/probable hits per source ('lakh', 'lakh_clean') and the number rejected."""
        by_md5: dict[str, list[tuple[str, str]]] = {}
        for md5, path, src in self._rows(work).values():
            by_md5.setdefault(md5, []).append((path, src))
        scored: dict[str, list[tuple[base.Match, str, str]]] = {}
        for md5, paths in by_md5.items():
            scored[md5] = [(base.match_name(p, work), p, src) for p, src in paths]
        title_hits = [m for m, lst in scored.items() if any(x[0].title_score >= TITLE_MIN for x in lst)]
        msd = self._msd_matches(work, title_hits)
        hits: dict[str, list[Hit]] = {s: [] for s in SOURCES}
        rejected = 0
        for md5 in title_hits:
            lst = scored[md5]
            acc = [x for x in lst if x[0].accepted]
            lmd = msd.get(md5)
            if acc:
                # Prefer a clean_midi reading (explicit Artist/Title), then the best score.
                m, path, src = max(acc, key=lambda x: (x[2] == "clean", x[0].title_score + x[0].artist_score))
                cls, ars = "accept", m.artist_score
            elif lmd:
                m, path, src = max((x for x in lst if x[0].title_score >= TITLE_MIN),
                                   key=lambda x: (x[2] == "clean", x[0].title_score))
                cls, ars = "probable", lmd[2]
            else:
                rejected += 1
                continue
            source = "lakh_clean" if src == "clean" else "lakh"
            hits[source].append(Hit(
                source=source, source_ref=md5, orig_name=path, title_score=m.title_score, artist_score=ars,
                match_class=cls, lmd_match_score=round(lmd[0], 4) if lmd else None,
                lmd_msd_id=lmd[1] if lmd else None, extra={"n_paths": len(lst)}))
        for s in hits:
            hits[s] = sorted(hits[s], key=Hit.sort_key)[:max_per_source]
        return hits, rejected

    def all_paths(self, md5: str) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT path FROM path WHERE md5=?", (md5,))]

    def extract(self, md5s: set[str]) -> Iterator[tuple[str, bytes]]:
        """Stream the tarballs once and yield (md5, original bytes) for the wanted files."""
        remaining = set(md5s)
        in_full = {m for m in remaining
                   if self.conn.execute("SELECT 1 FROM full_md5 WHERE md5=?", (m,)).fetchone()}
        if in_full:
            with tarfile.open(ensure_files(("lmd_full.tar.gz",))["lmd_full.tar.gz"], "r|gz") as tf:
                for ti in tf:
                    if not ti.isfile():
                        continue
                    md5 = ti.name.rsplit("/", 1)[-1].split(".", 1)[0]
                    if md5 in in_full:
                        f = tf.extractfile(ti)
                        if f is not None:
                            yield md5, f.read()
                            in_full.discard(md5)
                            remaining.discard(md5)
                            if not in_full:
                                break
        if remaining:
            names = {}
            for m in remaining:
                r = self.conn.execute("SELECT name FROM clean_member WHERE md5=? LIMIT 1", (m,)).fetchone()
                if r:
                    names[r[0]] = m
            if names:
                with tarfile.open(ensure_files(("clean_midi.tar.gz",))["clean_midi.tar.gz"], "r|gz") as tf:
                    for ti in tf:
                        if ti.name in names:
                            f = tf.extractfile(ti)
                            if f is not None:
                                yield names.pop(ti.name), f.read()
                                if not names:
                                    break


def fetch(conn: sqlite3.Connection, works: list[Work], *, max_per_source: int = base.MAX_PER_SOURCE,
          rebuild: bool = False, offline: bool = False, resanitize: bool = False,
          workers: int = max(1, (os.cpu_count() or 2) - 1)) -> list[SourceStats]:
    stats = {s: SourceStats(s) for s in SOURCES}
    idx = LakhIndex.open(rebuild=rebuild, offline=offline)
    # Searching is cheap (~50 ms per work), so every run searches every work again and skips
    # files already tried; an interrupted extraction pass therefore loses nothing.
    wanted: dict[str, list[tuple[Work, Hit]]] = {}
    t0 = time.monotonic()
    for w in works:
        hits, rejected = idx.search(w, max_per_source)
        for s in SOURCES:
            st = stats[s]
            st.works += 1
            st.hits += len(hits[s])
            st.matched_works += bool(hits[s])
            base.mark_searched(conn, w.work_id, s, len(hits[s]), rejected if s == "lakh" else 0)
            for h in hits[s]:
                if not resanitize and base.attempted(conn, w.work_id, s, h.source_ref):
                    continue
                wanted.setdefault(h.source_ref, []).append((w, h))
        stats["lakh"].rejected += rejected
    conn.commit()
    search_s = time.monotonic() - t0
    print(f"lakh: searched {stats['lakh'].works} works in {search_s:.1f} s; extracting {len(wanted)} files",
          flush=True)
    found: set[str] = set()

    def flush(batch: list[tuple[str, bytes]], pool: ProcessPoolExecutor | None) -> None:
        datas = [d for _, d in batch]
        procs = pool.map(midi.process, datas, chunksize=4) if pool else map(midi.process, datas)
        for (md5, data), proc in zip(batch, procs):
            for w, h in wanted[md5]:
                status = base.ingest(conn, w, h, data, replace=resanitize, processed=proc)
                stats[h.source].count(status, conn, w.work_id, md5)
        conn.commit()

    # Sanitizing is the slow part (~0.15 s per file); it runs in worker processes while
    # this process streams the tarball and writes the database.
    pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 and len(wanted) > 32 else None
    try:
        batch: list[tuple[str, bytes]] = []
        for md5, data in idx.extract(set(wanted)):
            found.add(md5)
            batch.append((md5, data))
            if len(batch) >= BATCH:
                flush(batch, pool)
                batch = []
        flush(batch, pool)
    finally:
        if pool is not None:
            pool.shutdown()
    for md5 in set(wanted) - found:
        for w, h in wanted[md5]:
            base.record_attempt(conn, w.work_id, h.source, md5, "download_failed", detail="not in archives")
            stats[h.source].count("download_failed")
    conn.commit()
    return [s.done() for s in stats.values()]
