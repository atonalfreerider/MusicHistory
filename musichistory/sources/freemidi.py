"""freemidi.org adapter: search, then the two-step session download (DESIGN.md §5).

Used only for works that still have fewer than 2 valid candidates after the offline
sources. Per work at most 3 requests, >= 3 s apart: ``/search?q=`` (cached), the song page
``download3-<id>-...`` (sets the PHP session; never cached, it may carry a lyrics block),
and ``getter-<id>`` with that page as Referer (the MIDI bytes; not cached). Without the
session the getter answers 302 to an HTML error page, so redirects are not followed.

The site search matches a substring of the song title ("use a word from the song title"),
so the query is the title alone. User uploads use the generic artist slug
``artists-bands`` and put the artist inside the title ("luis fonsi - despacito"); the
matcher reads both.
"""

from __future__ import annotations

import html
import re
import sqlite3
from html.parser import HTMLParser

from ..http import PoliteClient, RobotsDisallowed
from ..textnorm import fold
from . import base
from .base import Hit, SourceStats, Work

SOURCE = "freemidi"
HOST = "https://freemidi.org"
INTERVAL = 3.0
_SONG_HREF = re.compile(r"^/?download3-(\d+)-([a-z0-9-]*)$")
_GENERIC = {"artists", "artists bands", "national anthems", "tv themes", "movie themes", "video games",
            "seasonal", "random", "new"}


class _Cards(HTMLParser):
    """Collects (song href, link text, artist text, artist href) from search result cards."""

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[dict] = []
        self._in_title = self._in_text = False
        self._cur: dict | None = None
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "h5" and "card-title" in cls:
            self._in_title = True
        elif tag == "div" and "card-text" in cls and self._cur is not None:
            self._in_text = True
        elif tag == "a":
            href = (a.get("href") or "").strip()
            if self._in_title and _SONG_HREF.match(href.lstrip("/")):
                self._cur = {"href": href.lstrip("/"), "title": "", "artist": "", "artist_href": ""}
                self.cards.append(self._cur)
                self._capture = "title"
            elif self._in_text and self._cur is not None and not self._cur["artist_href"]:
                self._cur["artist_href"] = href
                self._capture = "artist"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._capture = None
        elif tag == "h5":
            self._in_title = False
        elif tag == "div" and self._in_text:
            self._in_text = False

    def handle_data(self, data: str) -> None:
        if self._capture and self._cur is not None:
            self._cur[self._capture] += data


def parse_search(page: str) -> list[dict]:
    p = _Cards()
    p.feed(page)
    for c in p.cards:
        c["title"] = html.unescape(c["title"]).strip()
        c["artist"] = html.unescape(c["artist"]).strip()
        m = _SONG_HREF.match(c["href"])
        c["id"] = m.group(1) if m else ""
    return [c for c in p.cards if c["id"]]


def query_for(title: str) -> str:
    """The title words, without words whose apostrophes the site may spell differently."""
    words = re.sub(r"[\(\)\[\]!?.,:;\"]", " ", _core_display(title)).split()
    runs, cur = [], []
    for w in words:
        if "'" in w or "’" in w:
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        runs.append(cur)
    best = max(runs, key=lambda r: sum(len(x) for x in r), default=[])
    return " ".join(best) or fold(title)


def _core_display(title: str) -> str:
    """Title without parentheticals and featuring credits, original spelling kept."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", title)
    t = re.sub(r"\s(?:feat\.?|featuring|ft\.?)\s.*$", "", t, flags=re.I)
    return t.strip() or title


def best_hit(cards: list[dict], work: Work) -> tuple[Hit | None, int]:
    hits, rejected = [], 0
    for c in cards:
        artist = c["artist"]
        generic = fold(artist) in _GENERIC or not c["artist_href"].lstrip("/").startswith("artist-")
        m = base.match_name(c["title"], work, artist_hint=None if generic else artist)
        if not m.accepted and not generic:
            m2 = base.match_name(c["title"], work)  # artist may still be in the title
            if m2.accepted:
                m = m2
        if m.accepted:
            hits.append(Hit(SOURCE, c["id"], f"{c['title']} ({'artists' if generic else artist})",
                            m.title_score, m.artist_score, "accept", url=f"{HOST}/{c['href']}",
                            extra={"href": c["href"]}))
        elif m.title_score >= base.TITLE_MIN:
            rejected += 1
    hits.sort(key=Hit.sort_key)
    return (hits[0] if hits else None), rejected


def fetch(conn: sqlite3.Connection, works: list[Work], client: PoliteClient, *, below: int = 2,
          refresh: bool = False) -> SourceStats:
    st = SourceStats(SOURCE)
    done = base.searched(conn, SOURCE) if not refresh else set()
    counts = base.valid_counts(conn)
    for w in works:
        if w.work_id in done or counts.get(w.work_id, 0) >= below:
            continue
        st.works += 1
        error = None
        hit, rejected = None, 0
        try:
            q = query_for(w.title)
            r = client.get(f"{HOST}/search", params={"q": q})
            st.requests += 0 if r.from_cache else 1
            if r.status == 200:
                hit, rejected = best_hit(parse_search(r.text), w)
            else:
                error = f"search HTTP {r.status}"
        except RobotsDisallowed:
            error = "robots"
        except Exception as exc:
            error = f"net:{type(exc).__name__}"
        st.rejected += rejected
        if hit is not None and not base.attempted(conn, w.work_id, SOURCE, hit.source_ref):
            st.hits += 1
            st.matched_works += 1
            data, detail = None, None
            try:
                page = client.get(hit.url or "", use_cache=False)
                st.requests += 1
                if page.status == 200:
                    g = client.get(f"{HOST}/getter-{hit.source_ref}", use_cache=False, allow_redirects=False,
                                   headers={"Referer": hit.url or HOST})
                    st.requests += 1
                    if g.status == 200 and g.content:
                        data = g.content
                        cd = g.headers.get("Content-Disposition", "")
                        fn = re.search(r'filename="?([^";]+)"?', cd)
                        if fn:
                            hit.orig_name = f"{fn.group(1).strip()} | {hit.orig_name}"
                    else:
                        detail = f"getter HTTP {g.status}"
                else:
                    detail = f"page HTTP {page.status}"
            except Exception as exc:
                detail = f"net:{type(exc).__name__}"
                error = detail
            if data is None:
                base.record_attempt(conn, w.work_id, SOURCE, hit.source_ref, "download_failed", detail=detail)
                st.count("download_failed")
            else:
                hit.url = f"{HOST}/getter-{hit.source_ref}"
                status = base.ingest(conn, w, hit, data)
                st.count(status, conn, w.work_id, hit.extra.get("md5"))
        elif hit is not None:
            st.matched_works += 1
        base.mark_searched(conn, w.work_id, SOURCE, int(hit is not None), rejected, error)
        conn.commit()
    return st.done()


def _page_url(conn: sqlite3.Connection, client: PoliteClient, song_id: str, work: Work, st: SourceStats) -> str | None:
    """The ``download3-<id>-...`` page of a stored song: from the request log, else the (cached) search."""
    r = conn.execute("SELECT url FROM fetch_log WHERE url LIKE ? ORDER BY id DESC LIMIT 1",
                     (f"{HOST}/download3-{song_id}-%",)).fetchone()
    if r:
        return r[0]
    s = client.get(f"{HOST}/search", params={"q": query_for(work.title)})
    st.requests += 0 if s.from_cache else 1
    if s.status == 200:
        for c in parse_search(s.text):
            if c["id"] == song_id:
                return f"{HOST}/{c['href']}"
    return None


def download(client: PoliteClient, page: str, song_id: str, st: SourceStats) -> tuple[bytes | None, str | None]:
    """The two-step session download: song page (sets the session), then the getter with Referer."""
    p = client.get(page, use_cache=False)
    st.requests += 1
    if p.status != 200:
        return None, f"page HTTP {p.status}"
    g = client.get(f"{HOST}/getter-{song_id}", use_cache=False, allow_redirects=False, headers={"Referer": page})
    st.requests += 1
    if g.status == 200 and g.content:
        return g.content, None
    return None, f"getter HTTP {g.status}"


def redownload(conn: sqlite3.Connection, works: list[Work], client: PoliteClient) -> SourceStats:
    """Download every stored freemidi candidate of ``works`` again and re-sanitize it in place.

    Same politeness as ``fetch``: >= 3 s per request, at most 3 requests per song (search,
    only when the song page is not in the request log and usually a cache hit; the song page,
    which sets the session; the getter with that page as Referer).
    """
    def get(row: sqlite3.Row, work: Work, st: SourceStats) -> tuple[bytes | None, str | None]:
        page = _page_url(conn, client, row["source_ref"], work, st)
        return download(client, page, row["source_ref"], st) if page else (None, "no song page")

    return base.redownload(conn, SOURCE, works, get)
