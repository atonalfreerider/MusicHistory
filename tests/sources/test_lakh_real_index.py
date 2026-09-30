"""Offline checks against the real Lakh index in data/cache (skipped when it is not built)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.sources import lakh  # noqa: E402
from musichistory.sources.base import Work  # noqa: E402

INDEX = ROOT / "data" / "cache" / "lakh" / "index.sqlite"
pytestmark = pytest.mark.skipif(not INDEX.exists(), reason="Lakh index not built (run fetch once)")


@pytest.fixture(scope="module")
def idx():
    return lakh.LakhIndex(INDEX)


def _all(hits):
    return [h for s in hits.values() for h in s]


def test_index_size(idx):
    m = idx.meta()
    assert int(m["n_paths"]) == 570_601 + int(m["n_clean"])
    assert int(m["n_msd"]) == 31_034


@pytest.mark.parametrize("title,artist,min_hits", [
    ("With or Without You", "U2", 8),
    ("(I Can't Get No) Satisfaction", "The Rolling Stones", 5),
    ("Bohemian Rhapsody", "Queen", 8),
    ("Hey Ya!", "OutKast", 1),
    ("Uptown Funk", "Mark Ronson featuring Bruno Mars", 1),
    ("Africa", "Toto", 8),
])
def test_known_songs_found(idx, title, artist, min_hits):
    hits, _ = idx.search(Work("X", title, artist, [artist]))
    assert len(_all(hits)) >= min_hits


def test_short_title_needs_the_artist(idx):
    hits, rejected = idx.search(Work("X", "Hero", "Enrique Iglesias", ["Enrique Iglesias"]))
    names = [h.orig_name.lower() for h in _all(hits)]
    assert names and rejected > 20  # hundreds of "Hero" files, most not Enrique's
    assert not any("mariah" in n or "carey" in n for n in names)
    for h in _all(hits):
        if h.match_class == "probable":
            assert h.lmd_msd_id and h.lmd_match_score >= 0.5


def test_post_2015_hits_are_missing(idx):
    hits, _ = idx.search(Work("X", "Blinding Lights", "The Weeknd", ["The Weeknd"]))
    assert _all(hits) == []
