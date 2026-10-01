"""Gather and rank the candidate photographs of one artist.

Sources, in order of trust:

1. ``p18`` — the artist item's Wikidata image(s) (P18); for a group that is its group photo.
2. ``depicts`` — Commons files whose structured data says they depict (P180) the artist.
3. ``category`` — files directly in the artist's Commons category (Wikidata P373) whose
   title names the artist.
4. ``leader`` — for a group with nothing usable: the P18 of the member who leads it, i.e.
   whose name is part of the group's name ("Bob Marley" of "Bob Marley & The Wailers").

Sources 2-4 are only consulted when no free P18 photo was taken within ``ERA_YEARS`` of one
of the artist's songs. Every candidate must be a bitmap (short side at least ``MIN_SIDE`` px,
``MIN_SIDE_P18`` for Wikidata's own image) with a
free licence (``licence.check``); search results must also not look like an album cover,
logo, signature, poster, grave, statue, memorabilia and the like (``BAD_TITLE``). The score
prefers Wikidata's choice, photos from the era of the songs (date metadata), and performing
or portrait shots.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import licence

MIN_SIDE = 300
MIN_SIDE_P18 = 200
ERA_YEARS = 10
IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/tiff", "image/webp"})
SOURCE_BONUS = {"p18": 2.5, "leader": 1.5, "depicts": 1.0, "category": 0.0}
MAX_CATEGORY_LOOKUPS = 50

BAD_TITLE = re.compile(
    r"\b(logo|logotype|wordmark|signatures?|autograph|sig|cover|covers|album|single|sleeve|vinyl|lp|discography"
    r"|poster|flyer|ticket|advert\w*|advertisement|ad|stamp|coin|banknote|grave|tomb|cemetery|memorial|statue"
    r"|sculpture|bust|mural|graffiti|painting|drawing|sketch|caricature|cartoon|illustration|wax|tussauds|plaque"
    r"|star|walk of fame|hall of fame|house|home|birthplace|museum|exhibit\w*|display|shirt|jacket|costume|dress"
    r"|guitar|car|map|sheet music|newspaper|news|magazine|billboard|screenshot|tribute|impersonator|lookalike"
    r"|cosplay|sticker|badge|street|road|sign|label|trophy|plate|book|letter|contract|manuscript|novelty"
    r"|radio(?! city)|dolls?|toys?|merchandise|disc)\b", re.I)
# A Commons category of the file that says it is not a photograph of a person (any source).
BAD_CATEGORY = re.compile(
    r"\b(drawings|paintings|murals|graffiti|caricatures|cartoons|comics|logos|album covers|cover art|sculptures"
    r"|statues|wax figures|dolls|toys|postage stamps|graves|signatures|fan art|illustrations|record sleeves"
    r"|vinyl records|vinyl singles?|78 rpm records|phonograph records|record labels|text logos|textlogo|memorials"
    r"|plaques|with trademark|pd ineligible|in art)\b", re.I)
# Also rejected for search results; a Wikidata image cropped from a trade-paper advert is
# often the only free photo of a 1970s band (it is a press photo), so P18 may be one.
BAD_SEARCH_CATEGORY = re.compile(r"\b(advertisements|posters|merchandise)\b", re.I)
# Wikidata's own image is trusted, except for what is plainly not a photo of the act.
BAD_P18_TITLE = re.compile(
    r"\b(logo|logotype|wordmark|signatures?|autograph|album|cover|sleeve|vinyl|single|poster|flyer|ticket"
    r"|ad|advert\w*|advertisement|stamp|grave|tomb|statue|wax|plaque)\b", re.I)
PERFORMING = re.compile(r"\b(perform\w*|live|concert|in concert|on stage|stage|tour|festival|gig|sings?|singing)\b", re.I)
PORTRAIT = re.compile(r"\b(portrait|publicity|press|promo\w*|headshot)\b", re.I)
CROPPED = re.compile(r"\bcrop(ped)?\b", re.I)

_DROP_TOKENS = frozenset({"the", "and", "his", "her", "orchestra", "band", "musical", "group", "singer", "musician"})


def tokens(text: str) -> set[str]:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", t) if w not in _DROP_TOKENS}


def title_core(title: str) -> str:
    """'File:Bob Marley 1976.jpg' -> 'Bob Marley 1976'."""
    t = re.sub(r"^File:", "", title or "")
    return re.sub(r"\.[A-Za-z0-9]{2,5}$", "", t).replace("_", " ")


def names_in_title(title: str, names: list[str]) -> bool:
    """Every significant word of one of ``names`` is a word of the title."""
    have = tokens(title_core(title))
    return any(n and tokens(n) and tokens(n) <= have for n in names)


@dataclass
class Artist:
    qid: str
    label: str
    work_ids: list[str] = field(default_factory=list)
    years: list[int] = field(default_factory=list)     # release years of those works
    names: list[str] = field(default_factory=list)     # credits naming the act


@dataclass
class Candidate:
    title: str
    source: str                     # p18 | depicts | category | leader
    info: dict
    subject: str
    subject_qid: str
    lic: licence.Licence | None = None
    year: int | None = None
    score: float = 0.0
    reason: str = ""                # why it was rejected ('' when usable)

    @property
    def ok(self) -> bool:
        return not self.reason


def era_score(year: int | None, ref_years: list[int]) -> float:
    if year is None or not ref_years:
        return 1.0
    d = min(abs(year - r) for r in ref_years)
    return max(0.0, 4.0 - d / 4.0)


def bad_category(ext: dict, search: bool = True) -> str | None:
    """The first category of the file that marks it as no photograph of a person
    (``search``: for a search result, which is judged more strictly than Wikidata's image)."""
    for cat in licence.meta(ext, "Categories").split("|"):
        if BAD_CATEGORY.search(cat) or (search and BAD_SEARCH_CATEGORY.search(cat)):
            return cat.strip()
    return None


def evaluate(c: Candidate, ref_years: list[int], latest: int = 2100, preferred: bool = False,
             names: list[str] | None = None) -> Candidate:
    """Fill in licence, year, score and rejection reason of a candidate. ``names``: the
    act's names; a search result titled with one of them scores higher (a group's
    depicting files include its members' solo shots)."""
    info = c.info or {}
    mime = info.get("mime", "")
    w, h = info.get("width") or 0, info.get("height") or 0
    ext = info.get("extmetadata") or {}
    core = title_core(c.title)
    cat = bad_category(ext, search=c.source not in ("p18", "leader"))
    if mime not in IMAGE_MIMES:
        c.reason = f"not a photograph ({mime or 'unknown type'})"
    elif min(w, h) < (MIN_SIDE_P18 if c.source in ("p18", "leader") else MIN_SIDE):
        c.reason = f"too small ({w}x{h})"
    elif not 0.4 <= w / h <= 2.5:
        c.reason = f"odd shape ({w}x{h})"
    elif not info.get("thumburl") and not info.get("url"):
        c.reason = "no file URL"
    elif (BAD_P18_TITLE if c.source in ("p18", "leader") else BAD_TITLE).search(core):
        c.reason = "not a photo of the artist (title)"
    elif cat:
        c.reason = f"not a photo of the artist (category {cat!r})"
    if c.reason:
        return c
    c.lic = licence.check(ext)
    if not c.lic.ok:
        c.reason = c.lic.reason
        return c
    c.year = licence.photo_year(ext, c.title, latest)
    s = SOURCE_BONUS.get(c.source, 0.0) + era_score(c.year, ref_years)
    if PERFORMING.search(core):
        s += 0.7
    elif PORTRAIT.search(core):
        s += 0.5
    if CROPPED.search(core):
        s += 0.3
    if preferred:
        s += 0.3
    if c.source == "depicts" and names and names_in_title(c.title, names):
        s += 0.5
    if min(w, h) < 500:
        s -= 0.5
    c.score = round(s, 3)
    return c


def best(cands: list[Candidate]) -> Candidate | None:
    ok = [c for c in cands if c.ok]
    return max(ok, key=lambda c: c.score) if ok else None


def needs_search(c: Candidate | None, ref_years: list[int]) -> bool:
    """Look beyond P18 unless a free P18 photo is known to be from the songs' era."""
    if c is None or c.year is None or not ref_years:
        return True
    return min(abs(c.year - r) for r in ref_years) > ERA_YEARS


def category_shortlist(titles: list[str], names: list[str], ref_years: list[int],
                       limit: int = MAX_CATEGORY_LOOKUPS) -> list[str]:
    """Category files worth an imageinfo lookup: titled with the artist's name, not a cover
    or memorabilia, bitmap extensions; titles with a year near the songs first."""
    keep = []
    for t in titles:
        core = title_core(t)
        if not re.search(r"\.(jpe?g|png|tiff?|webp)$", t, re.I) or BAD_TITLE.search(core):
            continue
        if not names_in_title(t, names):
            continue
        y = licence.year_in(re.sub(r"\(\d{6,}\)", "", core))
        rank = era_score(y, ref_years) + (0.7 if PERFORMING.search(core) else 0.5 if PORTRAIT.search(core) else 0)
        keep.append((-rank, t))
    keep.sort()
    return [t for _, t in keep[:limit]]


def leaders(group: dict, members: dict[str, dict]) -> list[dict]:
    """Members of a group whose name is part of the group's name ("Bob Marley" in "Bob
    Marley & The Wailers"), humans only."""
    gt = tokens(group.get("label", ""))
    out = []
    for q in group.get("P527", []):
        m = members.get(q) or {}
        mt = tokens(m.get("label", ""))
        if m and "Q5" in m.get("P31", []) and len(mt) >= 2 and mt <= gt:
            out.append(m)
    return out
