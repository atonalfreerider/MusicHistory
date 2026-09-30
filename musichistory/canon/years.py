"""Original-release year of each work, from conflicting evidence (pure functions).

Evidence (one ``year_evidence`` row each): Wikidata P577 of the work item and of the
recording items merged into it, MusicBrainz release dates, list years (tsort, Rolling
Stone, Grammy, Spotify, Acclaimed), Billboard year-end chart years and the first weekly
Hot 100 week. Chart years and chart weeks are *upper bounds* (the record existed by then).

Rules, in order (docs/DESIGN.md §4, plus refinements marked *):

1. Values before 1890 are ignored; an authoritative value (Wikidata/MusicBrainz) before
   1890, or an "unknown value" P577, marks the work ``traditional`` (its year then comes
   from the earliest MusicBrainz recording). P577 coarser than a year is ignored.
2. Upper bound U = the earliest of the first Hot 100 week and the last day of the earliest
   Billboard year-end chart year. An authoritative date in a later year than U is dropped;
   one later than U within U's year keeps its year but loses its month/day (*).
   (*) Optional, off in the stage: when every performer of the work is the famous one
   (``single_performer``), a Wikidata date 2 or more years before that performer's
   (corroborated) MusicBrainz release date and corroborated by nothing is taken for a
   Wikidata error ("Girls Like You" has P577 2011 for a 2017 single) and dropped.
   (*) A value more than 2 years before every other source and corroborated by none is a
   false match: a first Hot 100 week of a featured artist's own same-titled song, or a
   mis-dated MusicBrainz release; it is dropped (and the chart week is not a bound).
3. A list year more than 2 years before the earliest authoritative year A is dropped (a
   typo such as Rolling Stone's "Brass in Pocket" 1879), and so is one less than 2 years
   before A (*): Rolling Stone 2021 lists "What'd I Say" as 1957, Wikidata, MusicBrainz and
   the charts say 1959. (*) Either stays when another source family agrees with it within
   a year: tsort and the Grammy Hall of Fame both date "Boogie Chillen'" 1948 while
   Wikidata has a 1959 reissue. Only Wikidata and corroborated MusicBrainz dates define A.
4. ``work_year`` = the minimum of what remains (covers are later, so the minimum is the
   composition); ``work_date`` keeps month/day only from an authoritative source for that year.
5. ``year_confidence``: ``high`` when at least two independent source families agree
   within a year; ``review`` when nothing is left, when a traditional work has no
   MusicBrainz recording, or when the year rests on one source that a later independent
   source contradicts by more than 2 years; otherwise ``medium``.
6. ``effective_year`` (the famous recording's year) = the earliest evidence about the
   canonical recording, or ``work_year`` when the caller knows the famous performer is the
   original one (``canonical_is_original``).

The two performer-based options are not used by the stage: Wikidata's P175 usually lists
only the performers an article's infobox shows, so "every known performer is the famous
one" was true for many covers. Measured on the real data, the MusicBrainz rule overrode 76
Wikidata dates and most were covers' originals ("Bette Davis Eyes" 1974 -> 1981, "The
Entertainer" 1902 -> 1974), against a handful of real fixes ("Girls Like You").

MusicBrainz is consulted (``needs_musicbrainz``) when a work has no usable P577, when
P577 was dropped by a rule, when it is traditional, when a list year is earlier than P577,
or (single performer) when P577 is 2 or more years before everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_YEAR = 1890
AUTHORITATIVE = frozenset({"wikidata", "musicbrainz"})
UPPER_BOUND = frozenset({"billboard", "hot100"})


def family_of(source: str) -> str:
    """'list:billboard_ye_1975' -> 'billboard'; 'wikidata:P577:Q1' -> 'wikidata'."""
    if source.startswith("list:"):
        lid = source[5:]
        if lid.startswith("billboard_ye_"):
            return "billboard"
        if lid.startswith("rs500"):
            return "rolling_stone"
        return {"tsort_5000": "tsort", "grammy_hof": "grammy", "spotify_top": "spotify"}.get(lid, lid)
    return source.split(":")[0]


@dataclass
class Evidence:
    source: str
    value: str                 # ISO date at its precision: YYYY, YYYY-MM or YYYY-MM-DD
    precision: int = 9         # 9 year, 10 month, 11 day (lower = coarser than a year)
    accepted: bool = True
    reason: str = ""
    recording: bool = False    # evidence about one listed recording (list rows, MB by artist)
    canonical: bool = False    # about the canonical (highest-scoring) recording

    @property
    def family(self) -> str:
        return family_of(self.source)

    @property
    def year(self) -> int:
        return int(self.value[:5].rstrip("-")) if self.value[:1] == "-" else int(self.value[:4])

    @property
    def authoritative(self) -> bool:
        return self.family in AUTHORITATIVE

    def reject(self, reason: str) -> None:
        self.accepted = False
        self.reason = reason


@dataclass
class YearResult:
    work_year: int | None
    work_date: str | None
    work_date_precision: int | None
    effective_year: int | None
    year_confidence: str
    traditional: bool
    evidence: list[Evidence] = field(default_factory=list)
    needs_musicbrainz: str = ""   # why MusicBrainz should be asked ('' = not needed)
    chart_week_ok: bool = True    # False: the first Hot 100 week matched another song


def _upper_bound(evidence: list[Evidence], first_chart_week: str | None) -> str | None:
    bounds = [f"{e.year:04d}-12-31" for e in evidence if e.family == "billboard"]
    if first_chart_week:
        bounds.append(first_chart_week[:10])
    return min(bounds) if bounds else None


def _later_than(e: Evidence, bound: str) -> tuple[bool, bool]:
    """(later in year, later within the same year) for evidence ``e`` against ISO ``bound``."""
    by = int(bound[:4])
    if e.year > by:
        return True, False
    if e.year == by and e.precision >= 10 and e.value > bound[: len(e.value)]:
        return False, True
    return False, False


def decide(evidence: list[Evidence], first_chart_week: str | None = None, *, single_performer: bool = False,
           canonical_is_original: bool = False) -> YearResult:
    ev = [Evidence(**{**e.__dict__}) for e in evidence]  # never mutate the caller's objects
    for e in ev:
        e.accepted, e.reason = True, ""
    traditional = False
    capped: set[int] = set()

    def corroborated(e: Evidence) -> bool:
        return any(o is not e and o.accepted and o.family != e.family and abs(o.year - e.year) <= 1 for o in ev)

    # 1. unknown, coarse and pre-1890 values
    for e in ev:
        if e.value == "unknown":
            e.reject("unknown_value")
            traditional = traditional or e.family == "wikidata"
            continue
        if e.precision < 9:
            e.reject("precision_coarser_than_year")
            traditional = traditional or (e.authoritative and e.year < MIN_YEAR)
        elif e.year < MIN_YEAR:
            e.reject("before_1890")
            traditional = traditional or e.authoritative

    # 1b. a lone value years before everything else is a false match, not a release:
    # a featured artist's own same-titled hit on the Hot 100 ("Wild Wild West": Kool Moe
    # Dee 1988 vs Will Smith 1999) or a mis-dated MusicBrainz release.
    def lone_early(e: Evidence) -> bool:
        rest = [o.year for o in ev if o.accepted and o.family != e.family]
        return bool(rest) and e.year < min(rest) - 2 and not corroborated(e)

    chart_week_ok = True
    for e in ev:
        if e.accepted and e.source == "hot100:first_week" and lone_early(e):
            e.reject("uncorroborated_early_chart")
            chart_week_ok = False
    for e in ev:
        if e.accepted and e.source == "musicbrainz:recording" and lone_early(e):
            e.reject("uncorroborated_early_musicbrainz")

    # 2. upper bound from the charts
    bound = _upper_bound([e for e in ev if e.accepted], first_chart_week if chart_week_ok else None)
    if bound:
        for e in ev:
            if e.accepted and e.authoritative:
                later_year, later_date = _later_than(e, bound)
                if later_year:
                    e.reject("after_first_chart")
                elif later_date:
                    capped.add(id(e))
                    e.reason = "date_after_first_chart"
    if single_performer:  # same performer on MusicBrainz, years later: a Wikidata error
        mb_years = [e.year for e in ev if e.accepted and e.family == "musicbrainz" and corroborated(e)]
        if mb_years:
            for e in ev:
                if e.accepted and e.family == "wikidata" and e.year <= min(mb_years) - 2 and not corroborated(e):
                    e.reject("contradicted_by_musicbrainz")

    # 3. list years against the authoritative year (a MusicBrainz date counts only when
    # another source agrees: its relevance-ranked search can miss the first release)
    auth = [e.year for e in ev if e.accepted and (e.family == "wikidata" or (e.authoritative and corroborated(e)))]
    a_year = min(auth) if auth else None
    if a_year is not None:
        for e in ev:
            if not e.accepted or e.authoritative or e.family in UPPER_BOUND:
                continue
            if e.year < a_year and not corroborated(e):
                e.reject("list_year_before_authoritative" if e.year < a_year - 2 else "uncorroborated_early_list_year")

    # MusicBrainz is worth asking when the Wikidata date is missing or failed a check
    wd_all = [e for e in evidence if family_of(e.source) == "wikidata"]
    wd_ok = [e for e in ev if e.accepted and e.family == "wikidata"]
    needs = ""
    if traditional:
        needs = "traditional"
    elif not wd_all:
        needs = "no_P577"
    elif not wd_ok:
        needs = "P577_rejected"
    else:
        wd_year = min(e.year for e in wd_ok)
        others = [e.year for e in ev if e.accepted and e.family != "wikidata"]
        if any(e.family not in UPPER_BOUND and not e.authoritative and e.year < wd_year
               for e in evidence if e.value != "unknown" and e.precision >= 9 and e.year >= MIN_YEAR):
            needs = "list_year_before_P577"
        elif single_performer and others and wd_year <= min(others) - 2 and not any(
                corroborated(e) for e in wd_ok if e.year == wd_year):
            needs = "P577_uncorroborated"

    # 4. the year
    ok = [e for e in ev if e.accepted]
    if not ok:
        return YearResult(None, None, None, None, "review", traditional, ev, needs, chart_week_ok)
    work_year = min(e.year for e in ok)
    for e in ok:
        if e.year == work_year and not e.reason:
            e.reason = "min"
    same = [e for e in ok if e.year == work_year and e.authoritative and id(e) not in capped]
    if same:
        best = max(same, key=lambda e: (e.precision, -len(e.value)))
        prec = best.precision
        earliest = min(e.value for e in same if e.precision == prec)
        work_date, work_prec = earliest, prec
    else:
        work_date, work_prec = f"{work_year:04d}", 9

    # 5. confidence
    families = {e.family for e in ok if abs(e.year - work_year) <= 1}
    others = {}
    for e in ok:
        # a later MusicBrainz date nobody corroborates is usually a reissue, not a contradiction
        if e.family not in families and not (e.family == "musicbrainz" and not corroborated(e)):
            others[e.family] = min(others.get(e.family, 9999), e.year)
    if traditional and not any(e.family == "musicbrainz" for e in ok):
        conf = "review"
    elif len(families) >= 2:
        conf = "high"
    else:
        (fam,) = families
        auth_later = [y for f, y in others.items() if f in AUTHORITATIVE and y > work_year + 2]
        if fam in AUTHORITATIVE:
            conf = "review" if auth_later else "medium"
        elif auth_later or any(y > work_year + 2 for y in others.values()):
            conf = "review"
        else:
            conf = "medium"

    # effective year: the canonical recording's own year (never before the work)
    canon = [e.year for e in ok if e.canonical]
    effective = work_year if canonical_is_original or not canon else max(work_year, min(canon))
    return YearResult(work_year, work_date, work_prec, effective, conf, traditional, ev, needs, chart_week_ok)
