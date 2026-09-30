"""Resolve list rows to works (compositions).

Order of evidence, strongest first:

1. ``wiki_link``: the row links a Wikipedia article (Billboard, Grammy, Spotify pages).
   Titles go to QIDs through ``prop=pageprops`` in batches of 50 (redirects followed,
   ``#anchor`` stripped). A linked item must be typed as a song/single/composition/track;
   an album, artist or film link falls through to the next steps instead of merging every
   song that shares it.
2. ``key_match``: an unlinked row whose normalized title (``title_key`` or ``title_core``)
   and artist match an already resolved row borrows its work.
3. ``title_guess``: Wikipedia's naming conventions make most song articles "Title",
   "Title (song)" or "Title (Artist song)". Those guesses are looked up 50 per request
   (``pageprops``), a fraction of the cost of one search per row; a guess counts only when
   the item is a song whose performer (P175) or disambiguator names the row's artist, and
   its title matches the row's (or Wikipedia redirects the guessed title to it).

Titles compare by normalized keys with common spelling variants folded ("Mister"/"Mr.",
"Wanna"/"Want to", "'n'"/"and"); artists by fuzzy match or acronym ("CCR").
4. ``search``: MediaWiki full-text search for ``"<title> <artist> song"`` (CirrusSearch
   requires every word to occur in the article). A candidate must be song-typed and
   title-matching; it is taken with confidence 0.95 when a Wikidata performer or the
   article's disambiguator names the row's artist, else the first such candidate is taken
   with 0.75 unless its date is later than the row's year (a newer same-titled song is
   never used). Searches are rationed, because Wikimedia allows 10 requests a minute to a
   User-Agent without contact details: groups are searched best provisional score first,
   up to ``search_limit``.
5. ``normalized_key``: no QID; ``work_id = "R" + sha1(title_core|primary_artist)[:12]``.

Every QID is finally mapped through P2550 (recording or performance of) to the
composition, so a single's item and its song's item become one work.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable

from .. import textnorm as tn
from .model import Entry
from .wikidata import ALBUM, Wikidata, is_song, pub_dates

_ACT_SPLIT = re.compile(r"\s+(?:featuring|feat\.?|ft\.?|with|duet with|vs\.?|x)\s+|,|/|;", re.I)
_GENERIC_DISAMB = re.compile(
    r"^(?:\d{4}\s+)?(?:song|single|composition|instrumental|standard|hymn|carol|march|tango|"
    r"theme|jingle|poem|folk song|traditional song|nursery rhyme|record|recording)s?$", re.I)
_SMALL_WORDS = frozenset(
    "a an the and but or nor for so yet as at by in of off on per to up via from into onto over upon "
    "with than till".split())
_QUOTES = "\"'“”‘’("


def r_id(title: str, artist: str) -> str:
    """Work id for a row with no Wikidata item."""
    key = f"{tn.title_core(title)}|{tn.primary_artist(artist)}"
    return "R" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def first_act(artist: str) -> str:
    return _ACT_SPLIT.split(artist or "")[0].strip()


def _clean_title(title: str) -> str:
    return re.sub(r"[\"“”]", "", tn.strip_featuring(title)).strip()


def search_query(title: str, artist: str) -> str:
    a = re.sub(r"[\"“”]", "", first_act(artist))
    return f"{_clean_title(title)} {a} song".strip()


def mos_case(title: str) -> str:
    """Wikipedia title case: small words lower-case except first and last ("I Do It for You")."""
    words = title.split(" ")
    out = []
    for i, w in enumerate(words):
        j = 1 if w[:1] in _QUOTES else 0
        core = w.strip(_QUOTES + ")]!?,.")
        if 0 < i < len(words) - 1 and j == 0 and core.lower() in _SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append(w[:j] + w[j:j + 1].upper() + w[j + 1:])
    return " ".join(out)


def guess_titles(title: str, artist: str) -> list[str]:
    """Likely Wikipedia article titles for a song, most specific first."""
    t = _clean_title(title)
    a = re.sub(r"^the\s+", "", first_act(artist), flags=re.I).strip()
    a = re.sub(r"\s+(?:and|&)\s+(?:his|her|their)\s+(?:orchestra|band|comets)\b.*$", "", a, flags=re.I)
    out: list[str] = []
    for tv in dict.fromkeys([t, mos_case(t)]):
        for av in dict.fromkeys([a, " ".join(x[:1].upper() + x[1:] for x in a.split())]):
            if av:
                out.append(f"{tv} ({av} song)")
        out += [f"{tv} (song)", tv]
    return [x[0].upper() + x[1:] for x in dict.fromkeys(out)
            if x and not re.search(r"[\[\]{}<>|#]", x)]


def split_disamb(page: str) -> tuple[str, str]:
    """'Hello (Adele song)' -> ('Hello', 'Adele song')."""
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", page)
    return (m.group(1), m.group(2)) if m else (page, "")


def disamb_artist(disamb: str) -> str | None:
    """Performer named by a disambiguator ('The Beatles song' -> 'The Beatles'), if any."""
    d = disamb.strip()
    if not d or _GENERIC_DISAMB.match(d):
        return None
    m = re.match(r"^(.*?)\s+(?:song|single|composition|instrumental|recording)s?$", d, re.I)
    if not m:
        return None
    name = re.sub(r"^\d{4}\s+", "", m.group(1)).strip()
    return name or None


def _year(entity: dict | None) -> int | None:
    years = [int(iso[:4]) for iso, prec in pub_dates(entity) if prec >= 9 and iso[:4].isdigit()]
    return min(years) if years else None


# Spelling variants of the same title across lists ("Mister Sandman" / "Mr. Sandman",
# "Girls Just Wanna Have Fun" / "... Want to ...", "Rock 'n' Roll" / "Rock and Roll").
_TITLE_VARIANTS = [
    (re.compile(r"\bwanna\b"), "want to"), (re.compile(r"\bgonna\b"), "goin to"),
    (re.compile(r"\bgotta\b"), "got to"), (re.compile(r"\bgimme\b"), "give me"),
    (re.compile(r"\bmister\b"), "mr"), (re.compile(r"\bmissus\b"), "mrs"), (re.compile(r"\bsaint\b"), "st"),
    (re.compile(r"\bn\b"), "and"), (re.compile(r"\b(?:til|till)\b"), "until"),
] + [(re.compile(rf"\b{w}\b"), str(i)) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())]


def title_keys(title: str) -> set[str]:
    """Squashed normalized keys of a title, with common spelling variants folded together."""
    out: set[str] = set()
    for k in (tn.title_key(title), tn.title_core(title)):
        v = k
        for pat, rep in _TITLE_VARIANTS:
            v = pat.sub(rep, v)
        out |= {tn.squash(k), tn.squash(v)}
    return {k for k in out if k}


def same_title(a: str, b: str) -> bool:
    return bool(title_keys(a) & title_keys(b)) or tn.title_matches(a, b) >= 90


def acronym(name: str) -> str:
    return "".join(t[0] for t in tn.artist_key(name).split() if t != "and")


def same_artist(a: str, b: str) -> bool:
    """Artist credits naming the same act: fuzzy match, or an acronym ("CCR", "ELO")."""
    if tn.artist_matches(a, b) >= 85:
        return True
    sa, sb = tn.squash(tn.artist_key(a)), tn.squash(tn.artist_key(b))
    return (len(sa) >= 3 and sa == acronym(b)) or (len(sb) >= 3 and sb == acronym(a))



@dataclass
class Resolution:
    entities: dict[str, dict] = field(default_factory=dict)   # QID -> trimmed entity
    work_of: dict[str, str] = field(default_factory=dict)     # QID -> composition QID (P2550)
    labels: dict[str, str] = field(default_factory=dict)      # performer QID -> label
    stats: Counter = field(default_factory=Counter)


class Resolver:
    def __init__(self, wd: Wikidata, *, search: bool = True, log: Callable[[str], None] = print) -> None:
        self.wd = wd
        self.do_search = search
        self.log = log
        self.res = Resolution()

    def canonical(self, qid: str | None) -> str | None:
        if not qid:
            return None
        return self.res.entities.get(qid, {}).get("redirect_to", qid)

    def _assign(self, e: Entry, qid: str | None, method: str, conf: float) -> None:
        e.qid = qid
        work = self.res.work_of.get(qid, qid) if qid else None
        e.work_id = work
        e.resolution_method = method + ("+P2550" if qid and work != qid else "")
        e.resolution_confidence = conf
        self.res.stats[method] += 1

    def _load(self, qids: set[str]) -> None:
        """Claims for ``qids`` (and redirect targets), and their P2550 compositions."""
        missing = {q for q in qids if q not in self.res.entities}
        if missing:
            self.res.entities.update(self.wd.entities(missing))
        redirected = {e["redirect_to"] for e in self.res.entities.values() if e.get("redirect_to")}
        missing = redirected - set(self.res.entities)
        if missing:
            self.res.entities.update(self.wd.entities(missing))
        songs = [self.canonical(q) for q in qids if is_song(self.res.entities.get(self.canonical(q) or ""))]
        new = [q for q in songs if q and q not in self.res.work_of]
        if new:
            self.res.work_of.update(self.wd.follow_p2550(new, self.res.entities))
            targets = set(self.res.work_of.values()) - set(self.res.entities)
            if targets:  # a redirected P2550 target: claims of the canonical item
                self.res.entities.update(self.wd.entities(targets))

    def _load_labels(self, qids: set[str]) -> None:
        perf = {p for q in qids for p in self.res.entities.get(self.canonical(q) or "", {}).get("P175", [])
                if is_song(self.res.entities.get(self.canonical(q) or ""))}
        todo = perf - set(self.res.labels)
        if todo:
            self.res.labels.update(self.wd.labels(todo))

    def _artist_named(self, ent: dict, page: str, artist: str) -> bool:
        names = [self.res.labels.get(p, "") for p in ent.get("P175", [])]
        da = disamb_artist(split_disamb(page)[1])
        return any(n and same_artist(n, artist) for n in names) or bool(da and same_artist(da, artist))

    @staticmethod
    def _song_typed_as_album(ent: dict | None, page: str, title: str) -> bool:
        """A song article whose item is (wrongly) typed album: same title, no album disambiguator.

        Accepting album items in general would merge every song that links to the album.
        """
        if not ent or ALBUM not in ent.get("P31", ()):
            return False
        base, disamb = split_disamb(page)
        if re.search(r"\b(?:album|ep|soundtrack|film|musical)\b", disamb, re.I):
            return False
        return tn.title_matches(base, title) >= 95

    def _title_ok(self, ent: dict, page: str, title: str) -> bool:
        return same_title(split_disamb(page)[0], title) or same_title(ent.get("label", ""), title)

    @staticmethod
    def _groups(entries: list[Entry]) -> list[list[Entry]]:
        groups: dict[tuple[str, str], list[Entry]] = defaultdict(list)
        for e in entries:
            if not e.work_id:
                groups[(tn.squash(tn.title_key(e.raw_title)), tn.primary_artist(e.raw_artist))].append(e)
        return list(groups.values())

    # -- steps -----------------------------------------------------------------------------
    def by_links(self, entries: list[Entry]) -> None:
        links = sorted({e.wiki_link for e in entries if e.wiki_link})
        self.log(f"  resolve: {len(links)} linked articles -> QIDs (pageprops, 50 per request)")
        pp = self.wd.pageprops(links)
        qids = {v["qid"] for v in pp.values() if v.get("qid")}
        self.log(f"  resolve: {len(qids)} linked items -> Wikidata claims (50 per request)")
        self._load(qids)
        for e in entries:
            if not e.wiki_link or e.work_id:
                continue
            info = pp.get(e.wiki_link) or {}
            q = self.canonical(info.get("qid"))
            ent = self.res.entities.get(q or "")
            if q and is_song(ent):
                self._assign(e, q, "wiki_link", 1.0)
            elif q and self._song_typed_as_album(ent, info.get("page") or e.wiki_link, e.raw_title):
                self._assign(e, q, "wiki_link", 0.9)
                self.res.stats["wiki_link_album_typed"] += 1
            elif q:
                self.res.stats["link_not_song"] += 1
            else:
                self.res.stats["link_without_item"] += 1

    def by_keys(self, entries: list[Entry]) -> None:
        index: dict[str, list[Entry]] = defaultdict(list)
        for e in entries:
            if e.work_id and e.qid:
                for k in title_keys(e.raw_title):
                    index[k].append(e)
        for e in entries:
            if e.work_id:
                continue
            works: dict[str, Entry] = {}
            for k in title_keys(e.raw_title):
                for other in index.get(k, ()):
                    if other.work_id not in works and same_artist(other.raw_artist, e.raw_artist):
                        works[other.work_id] = other
            if len(works) == 1:
                other = next(iter(works.values()))
                self._assign(e, other.qid, "key_match", 0.9)
            elif len(works) > 1:
                self.res.stats["key_match_ambiguous"] += 1

    def by_titles(self, entries: list[Entry]) -> None:
        groups = self._groups(entries)
        guesses = [guess_titles(g[0].raw_title, g[0].raw_artist) for g in groups]
        titles = sorted({t for v in guesses for t in v})
        self.log(f"  resolve: {len(groups)} unlinked title/artist pairs -> {len(titles)} guessed article titles")
        pp = self.wd.pageprops(titles)
        qids = {v["qid"] for v in pp.values() if v.get("qid")}
        self.log(f"  resolve: {len(qids)} items behind guessed titles -> claims and performer labels")
        self._load(qids)
        self._load_labels(qids)
        for g, gs in zip(groups, guesses):
            title, artist = g[0].raw_title, g[0].raw_artist
            for guess in gs:
                info = pp.get(guess) or {}
                q = self.canonical(info.get("qid"))
                ent = self.res.entities.get(q or "")
                if not q or not is_song(ent):
                    continue
                page = info.get("page") or guess
                # a redirect from the guessed title is Wikipedia saying the titles are one song
                redirected = page != guess and split_disamb(page)[0] != split_disamb(guess)[0]
                if (redirected or self._title_ok(ent, page, title)) and self._artist_named(ent, page, artist):
                    for e in g:
                        self._assign(e, q, "title_guess", 0.9)
                    break

    def by_search(self, entries: list[Entry], limit: int | None = None,
                  priority: Callable[[Entry], float] | None = None) -> None:
        todo = self._groups(entries)
        if priority is not None:
            todo.sort(key=lambda g: -sum(priority(e) for e in g))
        if limit is not None and len(todo) > limit:
            self.res.stats["search_skipped_by_limit"] += sum(len(g) for g in todo[limit:])
            todo = todo[:limit]
        queries = [search_query(g[0].raw_title, g[0].raw_artist) for g in todo]
        cached = len(self.wd.kv.get_many("search", queries))
        self.log(f"  resolve: {len(todo)} title/artist pairs to search ({cached} cached)")
        results: list[list[dict]] = []
        for i, q in enumerate(queries):
            results.append(self.wd.search(q))
            if i % 100 == 99:
                self.log(f"    searched {i + 1}/{len(todo)}")
        cand: set[str] = set()
        for g, res in zip(todo, results):
            for r in res:
                if r.get("qid") and same_title(split_disamb(r["page"])[0], g[0].raw_title):
                    cand.add(r["qid"])
        self._load(cand)
        self._load_labels(cand)
        for g, res in zip(todo, results):
            year = min((e.list_year for e in g if e.list_year), default=None)
            pick = self.choose(res, g[0].raw_title, g[0].raw_artist, year)
            if pick:
                qid, conf, how = pick
                for e in g:
                    self._assign(e, qid, how, conf)
            else:
                self.res.stats["search_no_match"] += len(g)

    def choose(self, results: list[dict], title: str, artist: str, year: int | None) -> tuple[str, float, str] | None:
        ok: list[tuple[str, dict, str]] = []
        for r in results:
            q = self.canonical(r.get("qid"))
            ent = self.res.entities.get(q or "")
            if not q or not is_song(ent) or not self._title_ok(ent, r["page"], title):
                continue
            ok.append((q, ent, r["page"]))
        for q, ent, page in ok:  # strong: the artist is named by the item or the article
            if self._artist_named(ent, page, artist):
                return q, 0.95, "search"
        if ok:  # weak: first song-typed title match, never a song newer than the row
            q, ent, _ = ok[0]
            y = _year(ent)
            if year is None or y is None or y <= year + 1:
                return q, 0.75, "search_weak"
        return None

    def by_rid(self, entries: list[Entry]) -> None:
        for e in entries:
            if not e.work_id:
                e.qid = None
                e.work_id = r_id(e.raw_title, e.raw_artist)
                e.resolution_method = "normalized_key"
                e.resolution_confidence = 0.5
                self.res.stats["normalized_key"] += 1

    def run(self, entries: list[Entry], *, search_limit: int | None = None,
            priority: Callable[[Entry], float] | None = None) -> Resolution:
        self.by_links(entries)
        self.by_keys(entries)
        self.by_titles(entries)
        self.by_keys(entries)
        if self.do_search:
            self.by_search(entries, search_limit, priority)
            self.by_keys(entries)
        self.by_rid(entries)
        # Rows that resolved to a QID before its P2550 was known: remap to the composition.
        for e in entries:
            if e.qid and self.res.work_of.get(e.qid, e.qid) != e.work_id:
                e.work_id = self.res.work_of.get(e.qid, e.qid)
                if "+P2550" not in (e.resolution_method or ""):
                    e.resolution_method = (e.resolution_method or "") + "+P2550"
        return self.res
