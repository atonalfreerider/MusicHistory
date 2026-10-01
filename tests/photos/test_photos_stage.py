"""The photos stage end to end against a fake Wikidata/Commons (musichistory.photos)."""

from __future__ import annotations

import argparse
import importlib
import json
import sqlite3

import pytest
from photos_fakes import CC0, CC_BY_SA, FAIR_USE, NC, FakeWikimedia, entity, fake_resize, file

from musichistory import cli
from musichistory.http import Fetched
from musichistory.photos import catalogue, choose, stage
from musichistory.photos.choose import Artist
from musichistory.photos.commons import Wikimedia

AUTHOR_HTML = '<a href="//commons.wikimedia.org/wiki/User:Pat" title="User:Pat">Pat&nbsp;Photographer</a>'


def world():
    files = [
        # Ann: a free P18 from the era of her song -> taken without searching
        file("File:Ann Example 1985.jpg", **CC_BY_SA, Artist=AUTHOR_HTML, DateTimeOriginal="1985-06-01"),
        # The Inventions: non-free P18; a cover and a live shot among the depicting files
        file("File:The Inventions promo.jpg", **FAIR_USE, Artist="Label"),
        file("File:The Inventions album cover.jpg", **CC0),
        file("File:The Inventions live 1970.jpg", **CC_BY_SA, Artist=AUTHOR_HTML, DateTimeOriginal="1970-05-02"),
        file("File:The Inventions 1990.jpg", **CC0, DateTimeOriginal="1990"),
        # Carl: nothing free
        file("File:Carl Nobody.jpg", **NC, Artist="X"),
        file("File:Carl Nobody 1960.jpg", **NC, Artist="X"),
        file("File:Carl Nobody logo.svg", mime="image/svg+xml", **CC0),
        # Dee Leader & the Followers: no group image; the leader's portrait
        file("File:Dee Leader portrait.jpg", width=600, height=800, thumb_width=None, **CC0, DateTimeOriginal="1966"),
        # Eve: a recent P18, and an era photo in her category
        file("File:Eve Example 2015.jpg", **CC0, DateTimeOriginal="2015-01-01"),
        file("File:Eve Example performing 1962.jpg", **CC_BY_SA, Artist="Studio", DateTimeOriginal="1962-03-01"),
        file("File:Someone Else 1962.jpg", **CC0, DateTimeOriginal="1962"),
        # Fay: the P18 thumbnail cannot be downloaded; a depicting file can
        file("File:Fay Example.jpg", **CC0, DateTimeOriginal="1975"),
        file("File:Fay Example on stage.jpg", **CC0, DateTimeOriginal="1976"),
    ]
    ents = [
        entity("Q100", "Ann Example", p18=["Ann Example 1985.jpg"]),
        entity("Q200", "The Inventions", human=False, p18=["The Inventions promo.jpg"], p373="The Inventions"),
        entity("Q300", "Carl Nobody", p18=["Carl Nobody.jpg"], p373="Carl Nobody"),
        entity("Q400", "Dee Leader & the Followers", human=False, members=["Q401", "Q402"]),
        entity("Q401", "Dee Leader", p18=["Dee Leader portrait.jpg"]),
        entity("Q402", "Bo Follower"),
        entity("Q500", "Eve Example", p18=["Eve Example 2015.jpg"], p373="Eve Example (singer)"),
        entity("Q600", "Fay Example", p18=["Fay Example.jpg"]),
    ]
    return FakeWikimedia(
        ents, files,
        depicts={"Q200": ["File:The Inventions album cover.jpg", "File:The Inventions live 1970.jpg"],
                 "Q600": ["File:Fay Example on stage.jpg"]},
        categories={"Category:The Inventions": ["File:The Inventions 1990.jpg", "File:The Inventions album cover.jpg"],
                    "Category:Carl Nobody": ["File:Carl Nobody 1960.jpg", "File:Carl Nobody logo.svg"],
                    "Category:Eve Example (singer)": ["File:Eve Example performing 1962.jpg",
                                                      "File:Someone Else 1962.jpg"]},
        fail_downloads={"File:Fay Example.jpg"},
    )


def artists():
    return {a.qid: a for a in [
        Artist("Q100", "Ann Example", ["W1"], [1984], ["Ann Example"]),
        Artist("Q200", "The Inventions", ["W2", "W3"], [1971, 1972], ["The Inventions"]),
        Artist("Q300", "Carl Nobody", ["W4"], [1961], ["Carl Nobody"]),
        Artist("Q400", "Dee Leader & the Followers", ["W5"], [1965], ["Dee Leader and the Followers"]),
        Artist("Q500", "Eve Example", ["W6"], [1961], ["Eve Example"]),
        Artist("Q600", "Fay Example", ["W7"], [1975], ["Fay Example"]),
    ]}


@pytest.fixture()
def run_world(tmp_path):
    fake = world()
    wm = Wikimedia(tmp_path / "cache", http=fake, sleep=lambda s: None, log=lambda m: None)
    entries, misses = stage.collect(artists(), wm, tmp_path / "images", to_jpeg_fn=fake_resize, latest=2026,
                                    log=lambda m: None)
    return fake, wm, entries, misses, tmp_path


def test_choices(run_world):
    fake, wm, entries, misses, tmp = run_world
    got = {k: (e["commons_file"], e["source"]) for k, e in entries.items()}
    assert got == {
        "artist-Q100": ("File:Ann Example 1985.jpg", "p18"),
        "artist-Q200": ("File:The Inventions live 1970.jpg", "depicts"),
        "artist-Q400": ("File:Dee Leader portrait.jpg", "leader"),
        "artist-Q500": ("File:Eve Example performing 1962.jpg", "category"),
        "artist-Q600": ("File:Fay Example on stage.jpg", "depicts"),
    }
    assert [m["artist_qid"] for m in misses] == ["Q300"]
    reasons = {r["title"]: r["reason"] for r in misses[0]["rejected"]}
    assert "not free" in reasons["File:Carl Nobody.jpg"]
    assert "not free" in reasons["File:Carl Nobody 1960.jpg"]
    assert "File:Carl Nobody logo.svg" not in reasons  # filtered by extension before any lookup


def test_entries_follow_the_contract(run_world):
    fake, wm, entries, misses, tmp = run_world
    doc = catalogue.merge(catalogue.load(tmp / "images" / "artists.json"), entries)
    assert catalogue.validate(doc, tmp / "images") == []
    e = doc["images"]["artist-Q200"]
    assert e["file"] == "artists/artist-Q200.jpg"
    assert (e["subject"], e["artist_qid"], e["work_ids"]) == ("The Inventions", "Q200", ["W2", "W3"])
    assert e["author"] == "Pat Photographer"
    assert (e["license"], e["license_url"]) == ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0")
    assert e["commons_page"] == "https://commons.wikimedia.org/wiki/File:The_Inventions_live_1970.jpg"
    assert e["caption"] == "The Inventions in 1970"
    assert (e["width"], e["height"]) == (720, 540)  # the 960 px rendition re-encoded to 720
    assert e["year"] == 1970
    lead = doc["images"]["artist-Q400"]
    assert (lead["subject"], lead["artist_qid"], lead["caption"]) == ("Dee Leader", "Q400", "Dee Leader in 1966")
    assert (lead["width"], lead["height"]) == (600, 800)  # smaller than 720: kept as served
    ann = doc["images"]["artist-Q100"]
    assert ann["caption"] == "Ann Example in 1985"
    assert ann["license_url"].startswith("https://")
    assert list(doc["images"]) == sorted(doc["images"], key=lambda k: int(k.split("Q")[1]))


def test_requests_are_polite_and_cached(run_world, tmp_path):
    fake, wm, entries, misses, tmp = run_world
    api = [(u, p) for u, p in fake.calls if p]
    assert api and all(p.get("maxlag") == "5" and p.get("format") == "json" for _, p in api)
    # Ann's era P18 needs no search: no depicts or category request for Q100
    assert not any("P180=Q100" in p.get("gsrsearch", "") for _, p in api)
    assert any("P180=Q500" in p.get("gsrsearch", "") for _, p in api)  # Eve's P18 is from 2015
    # a second run answers every API question from the cache
    fake2 = world()
    wm2 = Wikimedia(tmp / "cache", http=fake2, sleep=lambda s: None, log=lambda m: None)
    entries2, _ = stage.collect(artists(), wm2, tmp / "images2", to_jpeg_fn=fake_resize, latest=2026,
                                log=lambda m: None)
    assert set(fake2.kinds()) == {"download"}
    assert entries2 == entries


def test_maxlag_is_retried():
    class Lagging:
        def __init__(self):
            self.n = 0

        def get(self, url, *, params=None, robots=True, use_cache=True, **kw):
            self.n += 1
            assert robots is False and use_cache is False
            if self.n == 1:
                return Fetched(url, 200, json.dumps({"error": {"code": "maxlag", "info": "lag"}}).encode(), "", {"Retry-After": "5"})
            return Fetched(url, 200, json.dumps({"entities": {"Q1": {"id": "Q1", "labels": {}}}}).encode(), "", {})

    import tempfile
    from pathlib import Path

    slept = []
    with tempfile.TemporaryDirectory() as d:
        wm = Wikimedia(Path(d), http=Lagging(), sleep=slept.append, log=lambda m: None)
        assert wm.entities(["Q1"]) == {"Q1": {"id": "Q1"}}
        wm.kv.close()
    assert slept == [5.0]


def test_artists_of_reads_the_singer_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE singer(work_id TEXT PRIMARY KEY, gender TEXT, source TEXT, artist_qid TEXT, artist_label TEXT)")
    conn.executemany("INSERT INTO singer VALUES (?,?,?,?,?)", [
        ("W1", "male", "p175:person", "Q1", "Roy Example"),
        ("W2", "male", "p175:person", "Q1", "Roy Example"),
        ("W3", "mixed", "p175:duet", "Q2;Q3", "Sean Example & Faith Example"),
        ("W4", "unknown", "unresolved", None, None),
    ])
    works = {"W1": {"year": 1960, "credit": "Roy Example"}, "W2": {"year": 1964, "credit": "Roy Example"},
             "W3": {"year": 1997, "credit": "Sean & Faith"}, "W4": {"year": 1950, "credit": "Nobody"},
             "W5": {"year": 1950, "credit": "Not in table"}}
    arts, unresolved = stage.artists_of(conn, works)
    assert list(arts) == ["Q1", "Q2", "Q3"]
    assert (arts["Q1"].work_ids, arts["Q1"].years) == (["W1", "W2"], [1960, 1964])
    assert (arts["Q2"].label, arts["Q3"].label, arts["Q3"].work_ids) == ("Sean Example", "Faith Example", ["W3"])
    assert [u["work_id"] for u in unresolved] == ["W4", "W5"]


def test_featured_works(tmp_path):
    p = tmp_path / "paths.json"
    p.write_text(json.dumps({"version": 2, "paths": [
        {"id": "a", "steps": [{"work_id": "W1", "artist": "X", "year": 1970}, {"work_id": "W2", "artist": "Y", "year": 1980}]},
        {"id": "b", "steps": [{"work_id": "W2", "artist": "Y", "year": 1980}]},
    ]}), encoding="utf-8")
    assert stage.featured_works(p) == {"W1": {"year": 1970, "credit": "X"}, "W2": {"year": 1980, "credit": "Y"}}


# ---------------------------------------------------------------------------- choosing rules
def _c(title, source="depicts", **kw):
    f = file(title, **{**CC0, **kw})
    return choose.Candidate(f["title"], source, f["info"], "Subject", "Q1")


@pytest.mark.parametrize("title", ["File:Subject album cover.jpg", "File:Subject logo.png", "File:Subject signature.jpg",
                                   "File:Subject grave.jpg", "File:Subject star on the Walk of Fame.jpg",
                                   "File:Subject wax figure at Madame Tussauds.jpg", "File:Subject single sleeve.jpg"])
def test_search_results_that_are_not_photos_of_the_artist_are_rejected(title):
    assert "title" in choose.evaluate(_c(title), [1970]).reason


@pytest.mark.parametrize("title, ok", [
    ("File:Subject Billboard 1977.jpg", True), ("File:Subject at the museum 1980.jpg", True),
    ("File:Subject Cash Box ad 1974.jpg", False), ("File:Subject logo.png", False), ("File:Subject album cover.jpg", False),
])
def test_wikidata_image_is_trusted_unless_plainly_not_a_photo(title, ok):
    c = choose.evaluate(_c(title, source="p18"), [1970])
    assert c.ok is ok
    assert (not ok) == ("title" in c.reason)
    assert ("title" in choose.evaluate(_c(title), [1970]).reason)  # search results are filtered harder


@pytest.mark.parametrize("source", ["p18", "depicts", "category"])
def test_drawings_murals_and_logos_are_rejected_by_category(source):
    drawing = _c("File:Subjecthanana.jpg", source=source, Categories="PD-self|Portraits of Subject|Drawings of women")
    assert "category 'Drawings of women'" in choose.evaluate(drawing, [2001]).reason
    signing = _c("File:Subject 2001.jpg", source=source, Categories="Musicians giving autographs|Subject in 2001")
    assert choose.evaluate(signing, [2001]).ok
    # a band photo cropped from a trade-paper advert: fine as Wikidata's image, not as a search hit
    crop = _c("File:Subject.png", source=source, Categories="Billboard advertisements|Subject")
    assert choose.evaluate(crop, [1970]).ok is (source == "p18")


def test_a_groups_depicting_file_titled_with_its_name_ranks_higher():
    names = ["The Inventions"]
    group = choose.evaluate(_c("File:The Inventions 2007 concert.jpg", DateTimeOriginal="2007"), [2007], names=names)
    member = choose.evaluate(_c("File:Ann Member concert.jpg", DateTimeOriginal="2007"), [2007], names=names)
    assert group.score == member.score + 0.5


def test_shape_size_and_type_filters():
    assert "not a photograph" in choose.evaluate(_c("File:S.svg", mime="image/svg+xml"), []).reason
    assert "too small" in choose.evaluate(_c("File:S.jpg", width=200, height=250), []).reason
    assert "odd shape" in choose.evaluate(_c("File:S.jpg", width=3000, height=600), []).reason


def test_era_and_kind_of_shot_rank_candidates():
    era = choose.evaluate(_c("File:Subject 1971.jpg", DateTimeOriginal="1971"), [1970])
    late = choose.evaluate(_c("File:Subject 2010.jpg", DateTimeOriginal="2010"), [1970])
    live = choose.evaluate(_c("File:Subject live 1971.jpg", DateTimeOriginal="1971"), [1970])
    assert live.score > era.score > late.score
    assert choose.needs_search(choose.evaluate(_c("File:S.jpg", source="p18", DateTimeOriginal="2010"), []), [1970])
    assert not choose.needs_search(choose.evaluate(_c("File:S.jpg", source="p18", DateTimeOriginal="1975"), []), [1970])
    assert choose.needs_search(choose.evaluate(_c("File:S.jpg", source="p18"), []), [1970])  # undated


def test_category_shortlist_needs_the_name():
    titles = ["File:Eve Example 1962.jpg", "File:Eve Example 2015.jpg", "File:Concert crowd.jpg",
              "File:Eve Example poster.jpg", "File:Eve Example.svg", "File:EVE EXAMPLE live.JPG"]
    assert choose.category_shortlist(titles, ["Eve Example"], [1961]) == [
        "File:Eve Example 1962.jpg", "File:EVE EXAMPLE live.JPG", "File:Eve Example 2015.jpg"]


def test_photos_stage_registered():
    module_name, help_text = cli.STAGES["photos"]
    assert module_name == "musichistory.photos.stage" and help_text
    mod = importlib.import_module(module_name)
    p = argparse.ArgumentParser()
    mod.add_arguments(p)
    args = p.parse_args([])
    assert (args.all, args.artist, args.refresh) == (False, None, False)
