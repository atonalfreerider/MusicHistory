"""Wikitext table parsing on fixture rows from several eras of the Billboard pages, the
Grammy Hall of Fame and the Spotify list."""

from __future__ import annotations

from pathlib import Path

import pytest

from musichistory.canon import billboard, grammy, spotify
from musichistory.canon import wikitext as wt

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def rows(year: int):
    return [(e.rank, e.raw_title, e.raw_artist, e.wiki_link, e.weight_factor)
            for e in billboard.parse_page(load(f"billboard_{year}.wikitext"), year)]


def test_1946_competition_rank_in_brackets_and_ties():
    r = rows(1946)
    assert r[0] == (1, "Prisoner of Love", "Perry Como", "Prisoner of Love (Russ Columbo song)", 1.0)
    assert [x[0] for x in r] == [1, 19, 19, 21, 30, 41]          # "20 (21)" -> 21, "35 (41)" -> 41
    assert r[4][2] == "Les Brown"                                # "[[Les Brown (bandleader) | Les Brown]]"
    assert r[5][1] == "(I Love You) For Sentimental Reasons"
    assert r[5][2] == "King Cole Trio"


def test_1956_rowspan_artist_is_carried_down():
    r = rows(1956)
    assert r[0][:3] == (1, "Heartbreak Hotel", "Elvis Presley")
    assert r[1][:3] == (2, "Don't Be Cruel", "Elvis Presley")    # artist from rowspan="2"
    assert r[2][:3] == (3, "Lisbon Antigua", "Nelson Riddle")
    assert r[3][3] == "Hound Dog (song)"
    assert r[4][1] == "Moonglow and Theme from Picnic"          # italics dropped


def test_1958_double_a_sides_split_with_b_side_half_weight():
    r = rows(1958)
    assert r[0] == (22, "Wear My Ring Around Your Neck", "Elvis Presley", "Wear My Ring Around Your Neck", 1.0)
    assert r[1] == (22, "Doncha' Think It's Time", "Elvis Presley", None, 0.5)   # unlinked B side
    assert r[2][1:] == ("Rockin' Robin", "Bobby Day", "Rockin' Robin (song)", 1.0)
    assert r[3][1:] == ("Over and Over", "Bobby Day", "Over and Over (Bobby Day song)", 0.5)
    assert len(r) == 6


def test_1972_data_sort_value_attributes_and_inner_quotes():
    r = rows(1972)
    assert r[0][1:3] == ("The Candy Man", "Sammy Davis Jr.")
    assert r[1][1:3] == ("Baby, Don't Get Hooked on Me", "Mac Davis")
    assert r[2][2] == "Melanie"
    assert r[3][1] == 'The Cover of "Rolling Stone"'
    assert r[3][3] == 'The Cover of "Rolling Stone"'


def test_2012_caption_and_featuring_credits():
    r = rows(2012)
    assert [x[0] for x in r] == [1, 3, 4]
    assert r[0][2] == "Gotye featuring Kimbra"
    assert r[1][2] == "Fun featuring Janelle Monáe"
    assert r[2][3] == "Payphone (song)"


def test_2025_rank_on_its_own_line():
    r = rows(2025)
    assert r[0][:3] == (1, "Die with a Smile", "Lady Gaga and Bruno Mars")
    assert r[1][:4] == (2, "Luther", "Kendrick Lamar and SZA", "Luther (song)")
    assert r[2][:3] == (9, "APT.", "Rosé and Bruno Mars")


def test_page_titles_by_era():
    assert billboard.page_title(1947) == "Billboard year-end top singles of 1947"
    assert billboard.page_title(1953) == "Billboard year-end top 30 singles of 1953"
    assert billboard.page_title(1957) == "Billboard year-end top 50 singles of 1957"
    assert billboard.page_title(1959) == "Billboard Year-End Hot 100 singles of 1959"


def test_grammy_keeps_singles_and_tracks_only():
    e = grammy.parse_page(load("grammy.wikitext"))
    assert [(x.raw_title, x.raw_artist, x.list_year) for x in e] == [
        ("3 O'Clock Blues", "B.B. King", 1951),
        ("Ain't Misbehavin'", 'Thomas "Fats" Waller', 1929),
        ("Afro-Cuban Jazz Suite", "Machito", 1950),
    ]
    assert e[1].wiki_link == "Ain't Misbehavin' (song)"
    assert [x.rank for x in e] == [1, 2, 3]


def test_spotify_row_header_song_cells_and_dates():
    e = spotify.parse_page(load("spotify.wikitext"))
    assert [(x.rank, x.raw_title, x.raw_artist, x.list_year, x.wiki_link) for x in e] == [
        (1, "Blinding Lights", "The Weeknd", 2019, "Blinding Lights"),
        (4, "Starboy", "The Weeknd and Daft Punk", 2016, "Starboy (song)"),
    ]


@pytest.mark.parametrize("src,expected", [
    ("[[A|B]] and [[C]]", "B and C"),
    ("{{sort|Beatles|The Beatles}}", "The Beatles"),
    ("{{sortname|Elvis|Presley}}", "Elvis Presley"),
    ("x<ref name=a>cite</ref> y<!-- hidden -->", "x y"),
    ("''[[Grease (film)|Grease]]''", "Grease"),
    ("[[File:X.jpg|thumb|caption]]Song", "Song"),
])
def test_plain(src, expected):
    assert wt.plain(src) == expected


def test_links_strip_anchor_and_skip_files():
    assert wt.links("[[Love Will Keep Us Together#Captain & Tennille version|Love]] [[File:a.jpg]]") == [
        "Love Will Keep Us Together"]


def test_colspan_and_nested_template_pipes():
    t = wt.parse_table('{|\n! A !! B !! C\n|-\n| colspan="2" | {{sort|x|wide}} || c\n|-\n| a || b || c\n|}')
    assert [[c.plain for c in r] for r in t.data_rows()] == [["wide", "wide", "c"], ["a", "b", "c"]]
    assert t.header_row() == ["a", "b", "c"]
