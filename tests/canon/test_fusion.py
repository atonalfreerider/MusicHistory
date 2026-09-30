"""Weighted reciprocal-rank fusion at work level, and the pool with its per-year floor."""

from __future__ import annotations

import pytest

from musichistory.canon.fusion import K, fuse, pool, rrf
from musichistory.canon.model import Entry, ListSource

SOURCES = {
    "billboard_ye_1965": ListSource("billboard_ye_1965", "bb 1965", 1.0),
    "billboard_ye_1990": ListSource("billboard_ye_1990", "bb 1990", 1.0),
    "tsort_5000": ListSource("tsort_5000", "tsort", 6.0),
    "rs500_2021": ListSource("rs500_2021", "rs", 4.0),
    "grammy_hof": ListSource("grammy_hof", "ghof", 1.5, pseudo_rank=250),
}


def entry(list_id, rank, title, artist, work, year=None, factor=1.0):
    return Entry(list_id, rank, title, artist, list_year=year, work_id=work, weight_factor=factor)


def test_rrf_formula():
    assert K == 60
    assert rrf(1.0, 1) == pytest.approx(1 / 61)
    assert rrf(6.0, 3) == pytest.approx(6 / 63)
    assert rrf(1.0, 10, factor=0.5) == pytest.approx(0.5 / 70)


def test_scores_sum_over_every_recording_and_list():
    entries = [
        entry("billboard_ye_1965", 4, "Unchained Melody", "The Righteous Brothers", "Q1"),
        entry("tsort_5000", 100, "Unchained Melody", "Righteous Brothers", "Q1"),
        entry("billboard_ye_1990", 20, "Unchained Melody", "The Righteous Brothers", "Q1"),   # re-entry
        entry("tsort_5000", 900, "Unchained Melody", "Les Baxter", "Q1"),                   # another recording
        entry("rs500_2021", 1, "Respect", "Aretha Franklin", "Q2"),
        entry("grammy_hof", 7, "Respect", "Otis Redding", "Q2"),                             # pseudo rank 250
    ]
    works = {w.work_id: w for w in fuse(entries, SOURCES)}
    q1 = 1 / 64 + 6 / 160 + 1 / 80 + 6 / 960
    q2 = 4 / 61 + 1.5 / 310
    assert works["Q1"].score == pytest.approx(q1)
    assert works["Q2"].score == pytest.approx(q2)
    assert q1 > q2 and works["Q1"].canon_rank == 1 and works["Q2"].canon_rank == 2
    # canonical recording = highest-scoring one; display spelling from Billboard over tsort
    assert works["Q1"].artist == "The Righteous Brothers"
    assert len(works["Q1"].recordings) == 2
    assert works["Q2"].artist == "Aretha Franklin"
    assert works["Q1"].lists_summary() == "billboard_ye_1965#4;billboard_ye_1990#20;tsort_5000#100"
    assert works["Q2"].lists_summary(frozenset({"grammy_hof"})) == "grammy_hof;rs500_2021#1"


def test_b_side_weight_and_deterministic_ties():
    entries = [
        entry("billboard_ye_1965", 10, "A side", "X", "RA"),
        entry("billboard_ye_1965", 10, "B side", "X", "RB", factor=0.5),
        entry("billboard_ye_1990", 10, "C", "Y", "RD"),
        entry("billboard_ye_1965", 10, "C2", "Z", "RC"),
    ]
    ranked = fuse(entries, SOURCES)
    assert ranked[-1].work_id == "RB" and ranked[-1].score == pytest.approx(0.5 / 70)
    assert [w.work_id for w in ranked[:3]] == ["RA", "RC", "RD"]   # equal scores: by work id


def test_pool_top_n_plus_per_year_floor():
    entries = [entry("tsort_5000", i, f"t{i}", "a", f"R{i:03d}") for i in range(1, 41)]
    ranked = fuse(entries, SOURCES)
    years = {w.work_id: (1990 if i < 30 else 1951 + (i % 3)) for i, w in enumerate(ranked)}
    chosen, extra = pool(ranked, years, 10, first_year=1950, last_year=1953, per_year=2)
    assert {w.work_id for w in ranked[:10]} <= chosen
    # 1951..1953 get 2 each from the tail, best first; 1950 has no works at all
    assert extra == {1951: 2, 1952: 2, 1953: 2}
    tail = [w.work_id for w in ranked[30:]]
    assert set(tail[:6]) <= chosen and not set(tail[6:]) & chosen
    assert len(chosen) == 16
