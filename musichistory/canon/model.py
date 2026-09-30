"""Plain data carried between the canon modules (no I/O here)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ListSource:
    """One ranked list (a ``source_list`` row)."""

    list_id: str
    name: str
    weight: float
    edition: str | None = None
    url: str | None = None
    revision: str | None = None
    sha256: str | None = None
    retrieved_at: str | None = None
    license_note: str | None = None
    pseudo_rank: int | None = None


@dataclass
class Entry:
    """One row of a list (a ``list_entry`` row), plus what resolution learns about it."""

    list_id: str
    rank: int
    raw_title: str
    raw_artist: str
    list_year: int | None = None
    wiki_link: str | None = None          # Wikipedia article title of the song, '#anchor' removed
    weight_factor: float = 1.0            # 0.5 for the B side of a double A-side
    artist_links: tuple[str, ...] = ()
    # filled by resolve
    qid: str | None = None                # Wikidata item the row resolved to (before following P2550)
    work_id: str | None = None
    resolution_method: str | None = None  # 'wiki_link' | 'key_match' | 'search' | 'normalized_key' (+ '+P2550')
    resolution_confidence: float | None = None


@dataclass
class Loaded:
    """A source module's output: the list metadata and its rows."""

    source: ListSource
    entries: list[Entry] = field(default_factory=list)
