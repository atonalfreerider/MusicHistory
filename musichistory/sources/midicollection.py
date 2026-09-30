"""midicollection.com adapter: offline sitemap index, direct file downloads (DESIGN.md §5).

robots.txt disallows the site search (``/*?q=``), so works are matched offline against the
slugs of the song sitemaps (``sitemap-songs-1..7.xml``, ~168k songs; slugs usually read
``<artist-slug>-<title-slug>``) and the artist sitemap (1,460 artist slugs, used to split
a slug into artist and title). The download filename is not in the slug: it is taken
from the artist page (``data-url="/midi/MIDI/<file>.mid"`` for every song it lists, one
request per artist) or, for songs not listed there, from the song page, whose JSON-LD
``byArtist`` also confirms the artist. Files are then fetched directly. Every request
goes through PoliteClient at >= 2 s per request; listing pages are cached on disk, MIDI
bytes are not.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

from .. import config
from ..http import PoliteClient, RobotsDisallowed
from ..textnorm import fold, squash
from . import base
from .base import Hit, SourceStats, Work

SOURCE = "midicollection"
HOST = "https://midicollection.com"
INTERVAL = 2.0
INDEX_VERSION = "1"
SITEMAP_MAX_AGE_DAYS = 7
_LOC = re.compile(r"/song/(\d+)/([^<\s]+)$")


def cache_dir() -> Path:
    return config.CACHE / "midicollection"


def _sitemap(client: PoliteClient | None, name: str, refresh: bool) -> bytes:
    path = cache_dir() / name
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < SITEMAP_MAX_AGE_DAYS * 86400
    if path.exists() and (fresh or not refresh or client is None):
        return path.read_bytes()
    if client is None:
        raise FileNotFoundError(f"{path} is missing (offline)")
    r = client.get(f"{HOST}/{name}", use_cache=False)
    if r.status != 200 or b"<urlset" not in r.content and b"<sitemapindex" not in r.content:
        raise RuntimeError(f"midicollection sitemap {name}: HTTP {r.status}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return r.content


def _locs(xml: bytes) -> list[str]:
    root = ET.fromstring(xml)
    return [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]


def build_index(client: PoliteClient | None, refresh: bool = False) -> Path:
    dest = cache_dir() / "index.sqlite"
    if dest.exists() and not refresh:
        return dest
    index = _locs(_sitemap(client, "sitemap.xml", refresh))
    names = [u.rsplit("/", 1)[-1] for u in index]
    songs: list[tuple[int, str]] = []
    artists: list[str] = []
    for name in names:
        if name.startswith("sitemap-songs"):
            for loc in _locs(_sitemap(client, name, refresh)):
                m = _LOC.search(loc)
                if m:
                    songs.append((int(m.group(1)), m.group(2)))
        elif name.startswith("sitemap-artists"):
            artists += [loc.rstrip("/").rsplit("/", 1)[-1] for loc in _locs(_sitemap(client, name, refresh))]
    tmp = dest.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    c = sqlite3.connect(tmp)
    c.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE song(id INTEGER PRIMARY KEY, slug TEXT NOT NULL, sq TEXT NOT NULL);
        CREATE VIRTUAL TABLE song_fts USING fts5(sq, content='song', content_rowid='id', tokenize='trigram');
        CREATE TABLE artist(slug TEXT PRIMARY KEY);
    """)
    c.executemany("INSERT OR REPLACE INTO song(id, slug, sq) VALUES (?,?,?)",
                  ((i, s, squash(fold(s))) for i, s in songs))
    c.execute("INSERT INTO song_fts(song_fts) VALUES ('rebuild')")
    c.executemany("INSERT OR IGNORE INTO artist(slug) VALUES (?)", ((a,) for a in artists))
    c.executemany("INSERT INTO meta VALUES (?,?)", [("version", INDEX_VERSION), ("n_songs", str(len(songs))),
                                                    ("n_artists", str(len(artists))),
                                                    ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S"))])
    c.commit()
    c.close()
    tmp.replace(dest)
    return dest


class MidiCollection:
    def __init__(self, client: PoliteClient | None, refresh_index: bool = False) -> None:
        self.client = client
        self.conn = sqlite3.connect(build_index(client, refresh_index))
        self.artists = sorted((r[0] for r in self.conn.execute("SELECT slug FROM artist")), key=len, reverse=True)
        self._artist_pages: dict[str, dict[int, str]] = {}
        self.requests = 0  # network requests made (disk-cache hits excluded)

    def _split(self, slug: str) -> tuple[str | None, str]:
        for a in self.artists:
            if slug.startswith(a + "-") and len(slug) > len(a) + 1:
                return a, slug[len(a) + 1:]
        return None, slug

    def search(self, work: Work, max_per_source: int = base.MAX_PER_SOURCE) -> tuple[list[Hit], int]:
        rows: dict[int, str] = {}
        for q in (t for t in work.title_forms if len(t) >= 3):
            for sid, slug in self.conn.execute(
                "SELECT s.id, s.slug FROM song_fts JOIN song s ON s.id = song_fts.rowid WHERE song_fts MATCH ?",
                (f'"{q}"',)):
                rows[sid] = slug
        hits, rejected = [], 0
        for sid, slug in rows.items():
            artist_slug, rest = self._split(slug)
            m = base.match_name(slug, work)
            if artist_slug:
                m2 = base.match_name(rest.replace("-", " "), work, artist_hint=artist_slug.replace("-", " "))
                if (m2.accepted, m2.title_score + m2.artist_score) > (m.accepted, m.title_score + m.artist_score):
                    m = m2
            if m.accepted:
                hits.append(Hit(SOURCE, str(sid), slug, m.title_score, m.artist_score, "accept",
                                url=f"{HOST}/song/{sid}/{slug}", extra={"artist_slug": artist_slug}))
            elif m.title_score >= base.TITLE_MIN:
                rejected += 1
        return sorted(hits, key=Hit.sort_key)[:max_per_source], rejected

    # -- network ------------------------------------------------------------------------
    def artist_page(self, artist_slug: str) -> dict[int, str]:
        """song id -> data-url for every song listed on an artist page (cached)."""
        if artist_slug in self._artist_pages:
            return self._artist_pages[artist_slug]
        out: dict[int, str] = {}
        assert self.client is not None
        r = self.client.get(f"{HOST}/artist/{artist_slug}")
        self.requests += int(not r.from_cache)
        if r.status == 200:
            for sid, url in re.findall(r'data-midi-id="(\d+)"\s+data-url="([^"]+)"', r.text):
                out[int(sid)] = html.unescape(url)
        self._artist_pages[artist_slug] = out
        return out

    def song_page(self, sid: str, slug: str) -> tuple[str | None, str | None]:
        """(data-url, JSON-LD artist name) from a song page (cached)."""
        assert self.client is not None
        r = self.client.get(f"{HOST}/song/{sid}/{slug}")
        self.requests += int(not r.from_cache)
        if r.status != 200:
            return None, None
        m = re.search(r'data-midi-id="%s"\s+data-url="([^"]+)"' % sid, r.text) or \
            re.search(r'<a class="btn" href="(/midi/[^"]+)"', r.text)
        artist = None
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S):
            try:
                d = json.loads(block)
            except json.JSONDecodeError:
                continue
            if d.get("@type") == "MusicRecording":
                artist = (d.get("byArtist") or {}).get("name")
        return (html.unescape(m.group(1)) if m else None), artist

    def download(self, data_url: str) -> bytes | None:
        assert self.client is not None
        r = self.client.get(HOST + data_url, use_cache=False)
        self.requests += 1
        return r.content if r.status == 200 else None


def fetch(conn: sqlite3.Connection, works: list[Work], client: PoliteClient, *,
          max_per_source: int = base.MAX_PER_SOURCE, refresh: bool = False, refresh_index: bool = False,
          below: int | None = None) -> SourceStats:
    st = SourceStats(SOURCE)
    mc = MidiCollection(client, refresh_index)
    done = base.searched(conn, SOURCE) if not refresh else set()
    counts = base.valid_counts(conn)
    for w in works:
        if w.work_id in done or (below is not None and counts.get(w.work_id, 0) >= below):
            continue
        st.works += 1
        hits, rejected = mc.search(w, max_per_source)
        st.rejected += rejected
        st.hits += len(hits)
        st.matched_works += bool(hits)
        error = None
        for h in hits:
            if base.attempted(conn, w.work_id, SOURCE, h.source_ref):
                continue
            try:
                artist_slug = h.extra.get("artist_slug")
                data_url = mc.artist_page(artist_slug).get(int(h.source_ref)) if artist_slug else None
                if data_url is None:
                    data_url, page_artist = mc.song_page(h.source_ref, h.orig_name)
                    if page_artist and max(base.artist_score(page_artist, a) for a in w.artists) < base.ARTIST_MIN:
                        base.record_attempt(conn, w.work_id, SOURCE, h.source_ref, "rejected", detail="page artist")
                        st.rejected += 1
                        continue
                if not data_url:
                    base.record_attempt(conn, w.work_id, SOURCE, h.source_ref, "download_failed", detail="no data-url")
                    st.count("download_failed")
                    continue
                data = mc.download(data_url)
            except RobotsDisallowed as exc:
                base.record_attempt(conn, w.work_id, SOURCE, h.source_ref, "download_failed", detail="robots")
                st.count("download_failed")
                error = str(exc)
                continue
            except Exception as exc:  # network trouble: retried on the next run
                base.record_attempt(conn, w.work_id, SOURCE, h.source_ref, "download_failed",
                                    detail=f"net:{type(exc).__name__}")
                st.count("download_failed")
                error = type(exc).__name__
                continue
            if data is None:
                base.record_attempt(conn, w.work_id, SOURCE, h.source_ref, "download_failed", detail="http")
                st.count("download_failed")
                continue
            h.url = HOST + data_url
            h.orig_name = unquote(data_url.rsplit("/", 1)[-1])
            status = base.ingest(conn, w, h, data)
            st.count(status, conn, w.work_id, h.extra.get("md5"))
            conn.commit()
        base.mark_searched(conn, w.work_id, SOURCE, len(hits), rejected, error)
        conn.commit()
    st.requests = mc.requests
    return st.done()
