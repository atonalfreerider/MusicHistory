"""Year rules: minimum over evidence, chart upper bound, list typos, traditional songs,
confidence and the effective (famous recording) year."""

from __future__ import annotations

from musichistory.canon.years import Evidence, decide, family_of


def ev(source, value, precision=9, **kw):
    return Evidence(source, value, precision, **kw)


def by_source(result):
    return {(e.source, e.value): (e.accepted, e.reason) for e in result.evidence}


def test_family_names():
    assert family_of("list:billboard_ye_1975") == "billboard"
    assert family_of("list:rs500_2021") == "rolling_stone"
    assert family_of("wikidata:P577:Q1") == "wikidata"
    assert family_of("hot100:first_week") == "hot100"


def test_cover_takes_the_original_year():
    # I Will Always Love You: composition 1974, Whitney Houston 1992/93
    r = decide([
        ev("wikidata:P577", "1974-06-06", 11),
        ev("list:tsort_5000", "1992", canonical=True),
        ev("list:billboard_ye_1993", "1993", canonical=True),
        ev("hot100:canonical", "1992-11-14", 11, canonical=True),
    ], first_chart_week="1974-06-01")
    # the first chart week is before the P577 day but in the same year: the year stays, the day goes
    assert r.work_year == 1974
    assert (r.work_date, r.work_date_precision) == ("1974", 9)
    assert r.effective_year == 1992
    assert r.year_confidence == "medium"


def test_wikidata_date_after_first_chart_is_dropped():
    r = decide([
        ev("wikidata:P577", "1977-03-01", 11),
        ev("list:billboard_ye_1975", "1975"),
        ev("list:tsort_5000", "1975"),
    ], first_chart_week="1975-02-08")
    assert by_source(r)[("wikidata:P577", "1977-03-01")] == (False, "after_first_chart")
    assert r.work_year == 1975
    assert r.year_confidence == "high"          # billboard + tsort
    assert r.needs_musicbrainz == "P577_rejected"


def test_list_typo_far_before_authoritative_is_dropped():
    # Rolling Stone 2021 lists "Brass in Pocket" as 1879
    r = decide([ev("wikidata:P577", "1979-11-16", 11), ev("list:rs500_2021", "1879"), ev("list:tsort_5000", "1980")])
    assert by_source(r)[("list:rs500_2021", "1879")] == (False, "before_1890")
    assert r.work_year == 1979 and r.work_date == "1979-11-16" and r.work_date_precision == 11
    assert r.year_confidence == "high"
    assert not r.traditional                     # a list year alone never makes a song traditional
    r = decide([ev("wikidata:P577", "1979-11-16", 11), ev("list:rs500_2021", "1970")])
    assert by_source(r)[("list:rs500_2021", "1970")] == (False, "list_year_before_authoritative")


def test_uncorroborated_early_list_year_is_dropped_but_corroborated_one_kept():
    # What'd I Say: RS2021 says 1957, everything else 1959
    evidence = [ev("wikidata:P577", "1959-06", 10), ev("list:rs500_2021", "1957"), ev("list:tsort_5000", "1959"),
                ev("list:billboard_ye_1959", "1959")]
    r = decide(evidence)
    assert by_source(r)[("list:rs500_2021", "1957")] == (False, "uncorroborated_early_list_year")
    assert r.work_year == 1959 and r.year_confidence == "high"
    assert r.needs_musicbrainz == "list_year_before_P577"
    # The Smiths: Wikidata has the 1992 single; the album (1986) is confirmed by MusicBrainz
    r = decide([ev("wikidata:P577", "1992-11", 10), ev("list:rs500_2021", "1986"),
                ev("musicbrainz:recording", "1986-06-16", 11)])
    assert r.work_year == 1986 and r.work_date == "1986-06-16" and r.year_confidence == "high"


def test_traditional_song_uses_earliest_recording():
    r = decide([ev("wikidata:P577", "1779", 9), ev("list:grammy_hof", "1947")])
    assert r.traditional and r.needs_musicbrainz == "traditional"
    assert r.work_year == 1947 and r.year_confidence == "review"     # no MusicBrainz recording yet
    r = decide([ev("wikidata:P577", "1779", 9), ev("list:grammy_hof", "1947"),
                ev("musicbrainz:earliest", "1922-03", 10)])
    assert r.work_year == 1922 and r.year_confidence == "medium"
    # an "unknown value" publication date also marks a traditional song
    r = decide([ev("wikidata:P577", "unknown"), ev("list:tsort_5000", "1964")])
    assert r.traditional and r.work_year == 1964


def test_coarse_precision_is_ignored():
    r = decide([ev("wikidata:P577", "1960", 8), ev("list:tsort_5000", "1965")])
    assert by_source(r)[("wikidata:P577", "1960")] == (False, "precision_coarser_than_year")
    assert r.work_year == 1965 and r.needs_musicbrainz == "P577_rejected"


def test_confidence_levels():
    assert decide([ev("wikidata:P577", "1965-08-15", 11), ev("list:tsort_5000", "1966")]).year_confidence == "high"
    assert decide([ev("list:tsort_5000", "1966")]).year_confidence == "medium"
    # one list year, contradicted by a much later independent source, and nothing authoritative
    assert decide([ev("list:tsort_5000", "1960"), ev("list:billboard_ye_1975", "1975")]).year_confidence == "review"
    # two authoritative sources disagreeing by more than 2 years (the later one corroborated)
    assert decide([ev("wikidata:P577", "1984"), ev("musicbrainz:recording", "1990"),
                   ev("list:tsort_5000", "1990")]).year_confidence == "review"
    # ...but a lone later MusicBrainz date is a reissue, not a contradiction
    assert decide([ev("wikidata:P577", "1984"), ev("musicbrainz:recording", "1990")]).year_confidence == "medium"
    # two lists agreeing on a year well before Wikidata's (a reissue date) keep it
    r = decide([ev("wikidata:P577", "1959"), ev("list:tsort_5000", "1948"), ev("list:grammy_hof", "1948")])
    assert r.work_year == 1948 and r.year_confidence == "high"
    r = decide([])
    assert r.work_year is None and r.year_confidence == "review"


def test_first_chart_week_is_evidence_and_upper_bound():
    week = ev("hot100:first_week", "1962-12-01", 11)
    r = decide([ev("list:tsort_5000", "1963"), week], first_chart_week="1962-12-01")
    assert r.work_year == 1962 and (r.work_date, r.work_date_precision) == ("1962", 9)   # a bound is not a date
    r = decide([ev("wikidata:P577", "1963-02", 10), ev("list:tsort_5000", "1963"), week],
               first_chart_week="1962-12-01")
    assert by_source(r)[("wikidata:P577", "1963-02")] == (False, "after_first_chart")
    assert r.work_year == 1962


def test_single_performer_wikidata_error_is_dropped_only_with_musicbrainz_proof():
    # "Girls Like You" (Maroon 5): Wikidata P577 2011-05-30 for a 2018 single
    base = [ev("wikidata:P577", "2011-05-30", 11), ev("list:billboard_ye_2018", "2018"),
            ev("hot100:first_week", "2018-06-09", 11)]
    r = decide(base, "2018-06-09", single_performer=True)
    assert r.work_year == 2011 and r.needs_musicbrainz == "P577_uncorroborated"
    r = decide(base + [ev("musicbrainz:recording", "2018-05-30", 11)], "2018-06-09", single_performer=True)
    assert by_source(r)[("wikidata:P577", "2011-05-30")] == (False, "contradicted_by_musicbrainz")
    assert r.work_year == 2018 and r.year_confidence == "high"
    # a cover keeps its early original date whatever the famous performer's MusicBrainz date says
    r = decide(base + [ev("musicbrainz:recording", "2018-05-30", 11)], "2018-06-09", single_performer=False)
    assert r.work_year == 2011
    # a sleeper hit whose old date is corroborated by a list keeps it
    r = decide([ev("wikidata:P577", "1985-08-05", 11), ev("list:tsort_5000", "1985"), ev("list:billboard_ye_2022", "2022"),
                ev("musicbrainz:recording", "2022-06-01", 11)], "2022-06-11", single_performer=True)
    assert r.work_year == 1985


def test_effective_year_for_original_performer_ignores_chart_reentries():
    evidence = [ev("wikidata:P577", "1964-11", 10), ev("list:billboard_ye_2022", "2022", canonical=True),
                ev("hot100:canonical", "2017-01-07", 11, canonical=True)]
    assert decide(evidence).effective_year == 2017
    assert decide(evidence, canonical_is_original=True).effective_year == 1964


def test_lone_early_chart_week_or_musicbrainz_date_is_a_false_match():
    # "Wild Wild West" (Will Smith feat. Kool Moe Dee, 1999) matched Kool Moe Dee's own 1988 hit
    r = decide([ev("hot100:first_week", "1988-04-30", 11), ev("list:billboard_ye_1999", "1999"),
                ev("list:tsort_5000", "1999"), ev("wikidata:P577", "1999-07", 10)], "1988-04-30")
    assert by_source(r)[("hot100:first_week", "1988-04-30")] == (False, "uncorroborated_early_chart")
    assert not r.chart_week_ok and r.work_year == 1999
    assert by_source(r)[("wikidata:P577", "1999-07")] == (True, "min")   # not dropped by a false bound
    # "(Sittin' On) The Dock of the Bay": a mis-dated MusicBrainz release
    r = decide([ev("musicbrainz:recording", "1964"), ev("list:rs500_2021", "1967"), ev("wikidata:P577", "1968-01-08", 11),
                ev("list:billboard_ye_1968", "1968")])
    assert by_source(r)[("musicbrainz:recording", "1964")] == (False, "uncorroborated_early_musicbrainz")
    assert r.work_year == 1967
    # an uncorroborated MusicBrainz date never removes list years ("Mister Sandman" 1954)
    r = decide([ev("list:tsort_5000", "1954"), ev("musicbrainz:recording", "2011")])
    assert r.work_year == 1954
    # the earliest recording of a traditional song is expected to predate everything
    r = decide([ev("wikidata:P577", "1779"), ev("list:grammy_hof", "1947"), ev("musicbrainz:earliest", "1927")])
    assert r.work_year == 1927


def test_input_is_not_mutated():
    e = ev("wikidata:P577", "1999", 9)
    decide([e, ev("list:billboard_ye_1990", "1990")])
    assert e.accepted and e.reason == ""
