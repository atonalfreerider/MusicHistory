"""MusicBrainz release dates for works whose Wikidata date is missing or suspect.

Recording search (``/ws/2/recording?query=recording:"T" AND artist:"A"``) returns each
recording's ``first-release-date`` (the earliest release carrying it). The earliest date
among close matches for the original or famous performer is a release date for the
work; for a traditional work, a title-only search gives the earliest recording of the
song at all. At most 1 request per second (MusicBrainz etiquette), every answer cached.
"""

from __future__ import annotations

import re

from .. import textnorm as tn
from .net import Net


def phrase(s: str) -> str:
    """A Lucene phrase: quotes and backslashes escaped."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _clean(title: str) -> str:
    return re.sub(r"[\"“”]", "", tn.strip_featuring(title)).strip()


def _precision(date: str) -> int:
    return {4: 9, 7: 10, 10: 11}.get(len(date), 9)


class MusicBrainz:
    def __init__(self, net: Net) -> None:
        self.net = net
        self.kv = net.kv

    def _search(self, query: str, limit: int) -> list[dict]:
        key = f"{limit}|{query}"
        hit = self.kv.get("mb_recording", key)
        if hit is not None or self.net.offline:
            return hit or []
        data = self.net.musicbrainz("recording", {"query": query, "limit": str(limit)})
        rows = []
        for rec in data.get("recordings", []):
            credit = "".join(a.get("name", "") + a.get("joinphrase", "") for a in rec.get("artist-credit", []))
            rows.append({"score": rec.get("score", 0), "title": rec.get("title", ""),
                         "date": rec.get("first-release-date", ""), "artist": credit, "id": rec.get("id")})
        self.kv.put("mb_recording", key, rows)
        return rows

    def cached(self, title: str, artist: str | None) -> bool:
        q = self._query(title, artist)
        return self.kv.get("mb_recording", f"{50 if artist else 100}|{q}") is not None

    @staticmethod
    def _query(title: str, artist: str | None) -> str:
        q = f"recording:{phrase(_clean(title))}"
        return f"{q} AND artist:{phrase(artist)}" if artist else q

    def earliest_by_artist(self, title: str, artist: str) -> tuple[str, int, str] | None:
        """(ISO date, precision, credited artist) of the earliest matching recording."""
        rows = self._search(self._query(title, artist), 50)
        ok = [r for r in rows if r["date"] and r["score"] >= 85 and tn.title_matches(r["title"], title) >= 90
              and tn.artist_matches(r["artist"], artist) >= 85]
        if not ok:
            return None
        best = min(ok, key=lambda r: r["date"])
        return best["date"], _precision(best["date"]), best["artist"]

    def earliest_any(self, title: str, min_year: int = 1890, before: int | None = None) -> tuple[str, int, str] | None:
        """Earliest recording of this exact title by anyone (for traditional songs).

        Search results are ordered by relevance, not date, so the query is limited to
        releases up to ``before`` (the earliest other evidence) to surface early recordings.
        """
        q = self._query(title, None)
        if before:
            q += f" AND firstreleasedate:[{min_year} TO {before}]"
        rows = self._search(q, 100)
        ok = [r for r in rows if r["date"][:4].isdigit() and int(r["date"][:4]) >= min_year and r["score"] >= 90
              and tn.title_matches(r["title"], title) >= 100]
        if not ok:
            return None
        best = min(ok, key=lambda r: r["date"])
        return best["date"], _precision(best["date"]), best["artist"]
