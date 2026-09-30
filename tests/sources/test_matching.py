"""Name matching decisions (DESIGN.md §5): accept, title-only and other-artist rejection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from musichistory.sources.base import Work, match_name, readings  # noqa: E402


def W(title: str, *artists: str) -> Work:
    return Work("X", title, artists[0], list(artists))


@pytest.mark.parametrize("work,name,expected", [
    # plain Artist/Title and Artist - Title
    (W("With or Without You", "U2"), "U2/With or Without You.mid", "accept"),
    (W("With or Without You", "U2"), "U2 - With Or Without You.mid", "accept"),
    # a bare leading "With" must survive normalization; 8.3-style squashed name without artist
    (W("With or Without You", "U2"), "w/withorwithoutyou2.mid", "title_only"),
    # parenthesised title: either the core or the full form matches
    (W("(I Can't Get No) Satisfaction", "The Rolling Stones"), "Rolling Stones/Satisfaction.mid", "accept"),
    (W("(I Can't Get No) Satisfaction", "The Rolling Stones"), "Rolling_Stones_-_I_Cant_Get_No_Satisfaction.mid", "accept"),
    (W("(I Can't Get No) Satisfaction", "The Rolling Stones"), "Satisfaction.mid", "title_only"),
    # punctuation in the title; slug and squashed forms
    (W("Hey Ya!", "OutKast"), "OutKast - Hey Ya.mid", "accept"),
    (W("Hey Ya!", "OutKast"), "outkast-hey-ya", "accept"),
    (W("Hey Ya!", "OutKast"), "UNSORTED MIDI/Outkast_Hey-ya.mid", "accept"),
    # featuring credits on either side
    (W("Uptown Funk", "Mark Ronson featuring Bruno Mars"), "mark-ronson-uptown-funk-feat-bruno-mars", "accept"),
    (W("Uptown Funk", "Mark Ronson featuring Bruno Mars"), "M/mark_ronson-uptown_funk_feat_bruno_mars.mid", "accept"),
    (W("Uptown Funk", "Mark Ronson featuring Bruno Mars"), "uptown-funk-kar-gc9", "title_only"),
    # inverted and upper-cased artist folders
    (W("What's Going On", "Marvin Gaye"), "Gaye, Marvin/Whats Going On.mid", "accept"),
    (W("What's Going On", "Marvin Gaye"), "GAYE MARVIN/What's Going On.mid", "accept"),
    # a cover under another artist is a different recording ...
    (W("What's Going On", "Marvin Gaye"), "Lauper, Cyndi/What's-Going-On.mid", "other_artist"),
    (W("Hero", "Enrique Iglesias"), "Mariah Carey/Hero.mid", "other_artist"),
    (W("Hero", "Mariah Carey"), "Enrique Iglesias/Hero.mid", "other_artist"),
    # ... unless that artist is one of the work's search artists
    (W("What's Going On", "Marvin Gaye", "Cyndi Lauper"), "Lauper, Cyndi/What's-Going-On.mid", "accept"),
    (W("Respect", "Aretha Franklin", "Otis Redding"), "Otis Redding/Respect.mid", "accept"),
    # short titles need artist evidence; longer words containing them are not the title
    (W("Hero", "Mariah Carey"), "hero.mid", "title_only"),
    (W("Hero", "Mariah Carey"), "mariahcareyhero.mid", "accept"),
    (W("Hero", "Mariah Carey"), "superheroes.mid", "no_title"),
    (W("Happy", "Pharrell Williams"), "Happy Together.mid", "no_title"),
    # a longer word that merely contains the artist is not the artist
    (W("Bohemian Rhapsody", "Queen"), "queensryche/Bohemian Rhapsody.mid", "other_artist"),
    (W("Bohemian Rhapsody", "Queen"), "bohemian-rhapsody-queen2-bb80", "accept"),
    (W("Africa", "Toto"), "africa_toto.kar", "accept"),
    (W("Despacito", "Luis Fonsi featuring Daddy Yankee"), "luis fonsi - despacito", "accept"),
    (W("Despacito", "Luis Fonsi featuring Daddy Yankee"), "Despacito", "title_only"),
])
def test_match_decisions(work, name, expected):
    m = match_name(name, work)
    assert m.reason == expected, (name, m)
    assert m.accepted == (expected == "accept")


def test_artist_hint_from_listing():
    w = W("Bohemian Rhapsody", "Queen")
    assert match_name("Bohemian Rhapsody", w, artist_hint="Queen").accepted
    assert match_name("Bohemian Rhapsody", w, artist_hint="Walt Disney").reason == "other_artist"


def test_readings_cover_common_layouts():
    r = readings("Sure.Polyphone.Midi/With or Without You - U 2.mid")
    assert ("U 2", "With or Without You") in r
    assert ("Sure.Polyphone.Midi", "With or Without You - U 2") in r
    assert (None, "With or Without You - U 2") in r


def test_squashed_artist_containing_feat_letters():
    # "daftpunk" contains "ft": the featuring-credit tail must not eat the artist
    w = W("Get Lucky", "Daft Punk")
    assert match_name("getluckydaftpunk.mid", w).accepted
    assert match_name("daft-punk-get-lucky-feat-pharrell-williams", w).accepted


def test_filler_matching_is_fast_on_adversarial_names():
    import time

    from musichistory.sources.base import _explained

    t0 = time.perf_counter()
    for s in ["1" * 40 + "zz", "k1" * 25 + "q", "v" * 55 + "x", "mix1" * 14 + "zz", "lk" * 29 + "zz"]:
        _explained(s, "")
        _explained(s, "queen")
    assert time.perf_counter() - t0 < 0.5
