"""Validation controls: the JSON holds only titles/artists/years, and control songs resolve
to works through normalized title + artist matching."""

from __future__ import annotations

import json

from musichistory.canon.known_influence import (ControlIndex, WorkKeys, control_rows, load_controls,
                                                wikidata_rows)
from musichistory.canon.resolve import disamb_artist, guess_titles, mos_case, r_id, search_query


def all_songs(c):
    for p in c["positive"]:
        yield p["src"]; yield p["dst"]
    yield from c["pachelbel_cluster"]["songs"]
    for fam in c["negative"]["families"].values():
        yield from fam
    for p in c["negative"]["pairs"]:
        yield p["src"]; yield p["dst"]
    for p in c["version"]:
        yield p["a"]; yield p["b"]


def test_controls_file_has_titles_artists_years_only():
    c = load_controls()
    songs = list(all_songs(c))
    assert len(c["positive"]) == 13 and len(c["pachelbel_cluster"]["songs"]) == 9
    assert len(c["version"]) == 6 and len(c["negative"]["pairs"]) == 3
    for s in songs:
        assert set(s) == {"title", "artist", "year"}
        assert isinstance(s["year"], int) and 1900 < s["year"] < 2030


WORKS = [
    WorkKeys("Q1", ["Surfin' USA"], ["The Beach Boys"], 0.05, 1963),
    WorkKeys("Q2", ["Sweet Little Sixteen"], ["Chuck Berry"], 0.03, 1958),
    WorkKeys("Q3", ["Don't Stop Believing"], ["Journey"], 0.06, 1981),
    WorkKeys("Q4", ["No Woman No Cry"], ["Bob Marley and the Wailers", "Bob Marley"], 0.04, 1974),
    WorkKeys("Q5", ["Hound Dog"], ["Elvis Presley", "Big Mama Thornton"], 0.09, 1952),
    WorkKeys("Q6", ["Hound Dog"], ["Rufus Thomas"], 0.001, 1953),   # same title, other song
    WorkKeys("Q7", ["Go West"], ["Village People", "Pet Shop Boys"], 0.01, 1979),
    WorkKeys("Q8", ["Basket Case"], ["Green Day"], 0.02, 1994),
    WorkKeys("Q9", ["(I Can't Get No) Satisfaction"], ["The Rolling Stones"], 0.1, 1965),
    WorkKeys("Q10", ["Let It Be"], ["The Beatles"], 0.08, 1970),
]


def test_normalized_title_and_artist_matching():
    idx = ControlIndex.build(WORKS)
    assert idx.find("Surfin' U.S.A.", "The Beach Boys") == "Q1"
    assert idx.find("Don't Stop Believin'", "Journey") == "Q3"
    assert idx.find("No Woman, No Cry", "Bob Marley & The Wailers") == "Q4"
    assert idx.find("Hound Dog", "Big Mama Thornton") == "Q5"      # through search_artists
    assert idx.find("Satisfaction", "Rolling Stones") == "Q9"      # title_core drops the parenthetical
    assert idx.find("Hound Dog", "Some Other Singer") is None       # title alone is not enough


def test_control_rows_kinds_directions_and_versions():
    rows, matches = control_rows(load_controls(), ControlIndex.build(WORKS))
    got = {(s, d, k): json.loads(n) for s, d, k, n in rows}
    assert ("Q2", "Q1", "control_positive") in got                 # Sweet Little Sixteen -> Surfin' U.S.A.
    assert got[("Q2", "Q1", "control_positive")]["channel"] == "melody,chord"
    assert ("Q7", "Q8", "control_positive") in got                 # Pachelbel cluster, earlier -> later
    assert got[("Q7", "Q8", "control_positive")]["group"] == "pachelbel_cluster"
    assert ("Q10", "Q3", "control_negative") in got               # I-V-vi-IV family, by year
    assert ("Q10", "Q4", "control_negative") in got
    assert got[("Q5", "Q5", "control_version")]["merged"] is True  # Thornton/Presley are one work
    assert {m.work_id for m in matches if m.title == "My Sweet Lord"} == {None}
    assert all(k in ("control_positive", "control_negative", "control_version") for _, _, k in got)


def test_wikidata_links_between_distinct_works_only():
    ents = {"Q20": {"P144": ["Q10"]}, "Q21": {"P2550": ["Q20"]}, "Q30": {"P144": ["Q99"]}}
    rows = wikidata_rows({"Q20": {"Q20", "Q21"}, "Q10": {"Q10"}, "Q30": {"Q30"}}, ents, {"Q21": "Q20"})
    assert [(s, d, k) for s, d, k, _ in rows] == [("Q10", "Q20", "wikidata_P144")]


def test_resolution_helpers():
    assert r_id("(I Can't Get No) Satisfaction", "The Rolling Stones") == r_id("Satisfaction", "Rolling Stones")
    assert r_id("Hello", "Adele") != r_id("Hello", "Lionel Richie")
    assert search_query("Uptown Funk", "Mark Ronson feat. Bruno Mars") == "Uptown Funk Mark Ronson song"
    assert mos_case("I Do it For You") == "I Do It for You"
    assert guess_titles("Yesterday", "The Beatles") == ["Yesterday (Beatles song)", "Yesterday (song)", "Yesterday"]
    assert disamb_artist("Adele song") == "Adele"
    assert disamb_artist("1945 song") is None and disamb_artist("song") is None
