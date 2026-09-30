"""midiworld.com adapter: last resort for works that still lack candidates (DESIGN.md §5).

Mostly a subset of midicollection. Search results read ``Title (Artist) - <a
href=".../download/<id>">``; files come straight from ``/download/<id>``. >= 3 s between
requests; the search page is cached, MIDI bytes are not. At most ``MAX_DOWNLOADS`` files
per work.
"""

from __future__ import annotations

import html
import re
import sqlite3

from ..http import PoliteClient, RobotsDisallowed
from . import base
from .base import Hit, SourceStats, Work
from .freemidi import query_for

SOURCE = "midiworld"
HOST = "https://www.midiworld.com"
INTERVAL = 3.0
MAX_DOWNLOADS = 3
_ITEM = re.compile(r"<li>\s*(.*?)\s*-\s*<a\s+href=\"(https?://www\.midiworld\.com/download/(\d+))\"", re.S | re.I)


def parse_search(page: str) -> list[dict]:
    out = []
    for text, url, did in _ITEM.findall(page):
        text = html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()
        m = re.match(r"^(.*)\(([^()]*)\)\s*$", text)
        title, artist = (m.group(1).strip(), m.group(2).strip()) if m else (text, "")
        out.append({"id": did, "url": url, "title": title, "artist": artist, "text": text})
    return out


def hits_for(items: list[dict], work: Work) -> tuple[list[Hit], int]:
    hits, rejected = [], 0
    for it in items:
        m = base.match_name(it["title"], work, artist_hint=it["artist"] or None)
        if m.accepted:
            hits.append(Hit(SOURCE, it["id"], it["text"][:200], m.title_score, m.artist_score, "accept",
                            url=it["url"]))
        elif m.title_score >= base.TITLE_MIN:
            rejected += 1
    return sorted(hits, key=Hit.sort_key)[:MAX_DOWNLOADS], rejected


def fetch(conn: sqlite3.Connection, works: list[Work], client: PoliteClient, *, below: int = 1,
          refresh: bool = False) -> SourceStats:
    st = SourceStats(SOURCE)
    done = base.searched(conn, SOURCE) if not refresh else set()
    counts = base.valid_counts(conn)
    for w in works:
        if w.work_id in done or counts.get(w.work_id, 0) >= below:
            continue
        st.works += 1
        error, hits, rejected = None, [], 0
        try:
            r = client.get(f"{HOST}/search/", params={"q": query_for(w.title)})
            st.requests += 0 if r.from_cache else 1
            if r.status == 200:
                hits, rejected = hits_for(parse_search(r.text), w)
            else:
                error = f"search HTTP {r.status}"
        except RobotsDisallowed:
            error = "robots"
        except Exception as exc:
            error = f"net:{type(exc).__name__}"
        st.rejected += rejected
        st.hits += len(hits)
        st.matched_works += bool(hits)
        for h in hits:
            if base.attempted(conn, w.work_id, SOURCE, h.source_ref):
                continue
            try:
                g = client.get(h.url or "", use_cache=False)
                st.requests += 1
            except Exception as exc:
                base.record_attempt(conn, w.work_id, SOURCE, h.source_ref, "download_failed",
                                    detail=f"net:{type(exc).__name__}")
                st.count("download_failed")
                continue
            if g.status != 200 or not g.content:
                base.record_attempt(conn, w.work_id, SOURCE, h.source_ref, "download_failed", detail=f"HTTP {g.status}")
                st.count("download_failed")
                continue
            fn = re.search(r'filename="?([^";]+)"?', g.headers.get("Content-Disposition", ""))
            if fn:
                h.orig_name = f"{fn.group(1).strip()} | {h.orig_name}"
            status = base.ingest(conn, w, h, g.content)
            st.count(status, conn, w.work_id, h.extra.get("md5"))
        base.mark_searched(conn, w.work_id, SOURCE, len(hits), rejected, error)
        conn.commit()
    return st.done()
