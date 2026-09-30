"""Lead-singer gender per work, from Wikidata (docs/DESIGN.md §12 "Singer").

``resolve_singers(conn, work_ids)`` fills the pipeline table ``singer`` and returns
``{work_id: {"gender", "source", "artist_qid", "artist_label"}}``. Gender is one of
``male | female | mixed | nonbinary | unknown | instrumental``.

**Lead act.** The recording's ``work.canonical_artist`` with featuring credits removed
(``textnorm.strip_featuring``). The whole credit is tried first ("Peter, Paul and Mary",
"Earth, Wind & Fire", "Simon & Garfunkel" are acts), then its first act (split on
"with", "x", "vs.", commas, slashes: "Jo Stafford with Paul Weston" -> "Jo Stafford").
Two more readings of that first act:

* *leader and band* — "Gladys Knight & the Pips", "Glenn Miller & his Orchestra",
  "Katrina and the Waves": the leader is the lead singer. When the band item is found, the
  leader is the member (P527) whose label or alias contains the leader's name; otherwise
  the leader is resolved as a person of their own.
* *duet* — "Elton John & Kiki Dee", "Les Paul and Mary Ford": each act is resolved;
  acts that do not sing (no singer occupation) are dropped when another one does, and the
  singing acts' genders combine (all equal -> that gender, else ``mixed``).

**Act -> Wikidata item**, strongest evidence first (and every candidate must be a human,
Q5, or a musical ensemble — any P31 class whose P279 chain reaches Q2088357 / Q215380 /
Q5741069, which covers bands, duos, girl groups, orchestras...):

1. ``p175``: a performer (P175) of the work's own Wikidata items — the composition, the
   recordings whose P2550 points at it, and the items behind the list rows' song links —
   whose label names the act. Read from canon's cache (``data/cache/canon/api_cache.sqlite``).
2. ``links``: the Wikipedia article linked from the artist cell of the work's Billboard,
   Grammy Hall of Fame or Spotify list row (canon's cached page wikitext), when the link
   text names the act; article -> QID through ``prop=pageprops``.
3. ``wikipedia``: the act's likely article titles ("Heart (band)", "Pink (singer)",
   "Prince (musician)", "Madonna", ...), 50 per ``pageprops`` request; a human must have a
   musical occupation.
4. ``search``: ``wbsearchentities`` on the name; the first human-with-a-musical-occupation
   or ensemble whose label/alias names the act.

**Gender.** Person: P21 (preferred-rank values first): male (and trans man, cisgender
male) -> ``male``; female (and trans woman, cisgender female) -> ``female``; any other
value (non-binary, genderfluid, ...) -> ``nonbinary``; none -> ``unknown``. Group: its
human members — P527 values, plus, when none of those sings, the items that state P463
"member of" the group (a ``haswbstatement`` search: many band items, e.g. Fleetwood Mac,
have no P527 at all). The first non-empty pool decides: members whose P527 qualifier says
"lead vocalist" (Q1045845); members with any vocal role qualifier; members who sing (P106
reaching singer Q177220, vocalist Q2643890, rapper Q2252262 or singer-songwriter Q488205
through P279, or instrument "voice"); every member. One gender -> that gender, else
``mixed``. No member at all -> the group's class (girl group -> ``female``, boy band ->
``male``), else ``unknown``.

**Instrumental** (conservative: both must hold):

* the MIDI has no sung text: the chosen candidate has no lyric event and no track whose
  name marks it as a vocal, and no other candidate of the work has >= 8 lyric events, a
  karaoke word track or a vocal-named track;
* Wikidata says so, for the song — an item of the work typed "music track without lyrics"
  (Q55850643) or "instrumental composition" (Q24887304), with genre (P136) "instrumental
  music" (Q639197) or "instrumental rock" (Q1650296), described as an "instrumental", or
  whose English article is disambiguated "(instrumental)" — or for the act: an
  "instrumental ensemble" (Q11072804), an act whose genre is one of those two, or an act
  nobody in which sings (a person whose occupations include no singing one, a group none
  of whose members sings) when no item of the work is typed "song" (Q7366, a composition
  for voices) or credits a lyricist (P676). An item typed "music track with vocals"
  (Q55850593) vetoes it.

**Network.** Wikidata/Wikipedia API via ``musichistory.http.PoliteClient``: sequential,
Wikipedia and Wikidata share one rate budget (canon's interval: 6.5 s without
``MUSICHISTORY_CONTACT``, 0.7 s with it), ``maxlag=5`` on Wikidata, ``wbgetentities`` in
batches of <= 50 ids, ``pageprops`` in batches of <= 50 titles. Every answer is cached in
``data/cache/themes/wikidata.sqlite`` (canon's caches are only read). ``refresh=True``
re-fetches what this run needs instead of reading that cache.

Run for real: ``python -m musichistory.themes.singer`` (every selected work).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import config
from .. import textnorm as tn
from ..canon.net import KVCache, WIKIDATA_API, WIKIMEDIA_INTERVAL, WIKIPEDIA_API
from ..canon.resolve import same_artist, split_disamb

GENDERS = ("male", "female", "mixed", "nonbinary", "unknown", "instrumental")

SCHEMA = """
CREATE TABLE IF NOT EXISTS singer(
  work_id TEXT PRIMARY KEY,
  gender TEXT NOT NULL,        -- male | female | mixed | nonbinary | unknown | instrumental
  source TEXT NOT NULL,        -- '<how the act was found>:<how the gender was derived>', e.g.
                               -- 'p175:person', 'links:leader', 'p175:group-singers', 'p175:duet',
                               -- 'p175:instrumental-song-genre'; 'unresolved' when no act was found
  artist_qid TEXT,             -- Wikidata item of the lead act (duets: items joined by ';')
  artist_label TEXT            -- its English label (duets: labels joined by ' & ')
);
"""

THEMES_CACHE = config.CACHE / "themes"
ENT = "ent.v3"      # cache namespace of trimmed entities (v3: "mul" labels, membership spans)
MEMBERS = "p463"    # cache namespace of inverse member-of searches
CANON_CACHE = config.CACHE / "canon"
BATCH = 50

# Wikidata ids (labels checked with wbgetentities on 2026-09-30).
HUMAN = "Q5"
ENSEMBLE_ROOTS = frozenset({"Q2088357", "Q215380", "Q5741069"})  # musical ensemble / group, rock band
GIRL_GROUP, BOY_BAND = "Q641066", "Q216337"
VOCAL_ROOTS = frozenset({"Q177220", "Q2643890", "Q2252262", "Q488205"})  # singer, vocalist, rapper, s-s
MUSIC_ROOTS = VOCAL_ROOTS | {
    "Q639669",   # musician
    "Q36834",    # composer
    "Q753110",   # songwriter
    "Q183945",   # record producer
    "Q130857",   # disc jockey
    "Q1278335",  # instrumentalist
    "Q158852",   # conductor
    "Q806349",   # bandleader
}
VOICE = "Q17172850"  # instrument (P1303) "voice"
LEAD_VOCALIST, FRONT_PERSON = "Q1045845", "Q1004105"  # P527 member role qualifiers
MALE_IDS = frozenset({"Q6581097", "Q2449503", "Q15145778"})    # male, trans man, cisgender male
FEMALE_IDS = frozenset({"Q6581072", "Q1052281", "Q15145779"})  # female, trans woman, cisgender female
INSTRUMENTAL_TRACK_TYPES = frozenset({"Q55850643", "Q24887304"})  # track without lyrics, instr. composition
INSTRUMENTAL_GENRES = frozenset({"Q639197", "Q1650296"})         # instrumental music, instrumental rock
INSTRUMENTAL_ENSEMBLE = "Q11072804"
VOCAL_TRACK = "Q55850593"                                        # music track with vocals
SONG = "Q7366"                                                   # song: composition for voice(s)

_STOP_CLASSES = ENSEMBLE_ROOTS | MUSIC_ROOTS | {HUMAN, GIRL_GROUP, BOY_BAND, INSTRUMENTAL_ENSEMBLE}

CLASS_DEPTH = 5         # P279 steps followed when testing a class against the roots above
LYRIC_EVENTS_MIN = 8     # lyric events in a non-chosen candidate that count as sung text

PROPS = ("P31", "P279", "P106", "P463", "P136", "P1303", "P175", "P2550", "P676")
_INSTRUMENTAL_DESC = re.compile(r"\binstrumental\b(?!\s+(?:version|in\b))", re.I)

Log = Callable[[str], None]


class ApiError(RuntimeError):
    pass


# ---------------------------------------------------------------------------- entities
def _snak_id(snak: dict) -> str | None:
    if snak.get("snaktype") != "value":
        return None
    v = snak.get("datavalue", {}).get("value")
    return v.get("id") if isinstance(v, dict) else None


def trim(entity: dict) -> dict:
    """The fields this module reads from a wbgetentities entity (deprecated claims dropped).

    Labels and aliases are English, else the language-independent "mul" ones (many person
    items carry their name only as a "mul" label)."""
    out: dict[str, Any] = {"id": entity.get("id")}
    labels = entity.get("labels", {})
    label = (labels.get("en") or labels.get("mul") or {}).get("value")
    if label:
        out["label"] = label
    desc = (entity.get("descriptions", {}).get("en") or {}).get("value")
    if desc:
        out["desc"] = desc
    al = entity.get("aliases", {})
    aliases = [a.get("value") for a in al.get("en", []) + al.get("mul", []) if a.get("value")]
    if aliases:
        out["aliases"] = aliases[:20]
    enwiki = (entity.get("sitelinks", {}).get("enwiki") or {}).get("title")
    if enwiki:
        out["enwiki"] = enwiki
    claims = entity.get("claims", {})
    for p in PROPS:
        vals = [q for st in claims.get(p, []) if st.get("rank") != "deprecated"
                for q in [_snak_id(st.get("mainsnak", {}))] if q]
        if vals:
            out[p] = list(dict.fromkeys(vals))
    genders = [[q, st.get("rank", "normal")] for st in claims.get("P21", []) if st.get("rank") != "deprecated"
               for q in [_snak_id(st.get("mainsnak", {}))] if q]
    if genders:
        out["P21"] = genders
    members = []
    for st in claims.get("P527", []):
        q = _snak_id(st.get("mainsnak", {}))
        if not q or st.get("rank") == "deprecated":
            continue
        quals = st.get("qualifiers", {})
        roles = [r for p in ("P2868", "P3831") for s in quals.get(p, []) for r in [_snak_id(s)] if r]
        m = {"id": q, **({"roles": roles} if roles else {})}
        span = _span(quals)
        if span != [None, None]:
            m["span"] = span
        members.append(m)
    if members:
        out["P527"] = members
    spans = {}  # member of (P463) group -> [start year, end year] when qualified
    for st in claims.get("P463", []):
        q = _snak_id(st.get("mainsnak", {}))
        if q and st.get("rank") != "deprecated":
            span = _span(st.get("qualifiers", {}))
            if span != [None, None]:
                spans[q] = span
    if spans:
        out["P463_span"] = spans
    return out


def _year_of(snaks: list[dict]) -> int | None:
    for s in snaks:
        v = s.get("datavalue", {}).get("value") if s.get("snaktype") == "value" else None
        m = re.match(r"^([+-]?\d{1,4})-", (v or {}).get("time", "") if isinstance(v, dict) else "")
        if m:
            return int(m.group(1))
    return None


def _span(quals: dict) -> list[int | None]:
    """[start year, end year] of a membership statement (P580 / P582 qualifiers)."""
    return [_year_of(quals.get("P580", [])), _year_of(quals.get("P582", []))]


def active(span: list[int | None] | None, year: int | None) -> bool:
    """A membership span covers ``year`` (a member who joined the year after still counts:
    release years and membership dates are both approximate)."""
    if not span or year is None:
        return True
    start, end = span
    return (start is None or start <= year + 1) and (end is None or end >= year)


class CanonCache:
    """Read-only view of canon's API cache (the canon stage owns and writes it)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or CANON_CACHE)
        path = self.root / "api_cache.sqlite"
        self.conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) if path.exists() else None

    def get_many(self, ns: str, keys: Iterable[str]) -> dict[str, Any]:
        keys = list(dict.fromkeys(k for k in keys if k))
        out: dict[str, Any] = {}
        if self.conn is None:
            return out
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = f"SELECT key, value FROM kv WHERE ns = ? AND key IN ({','.join('?' * len(chunk))})"
            out.update((k, json.loads(v)) for k, v in self.conn.execute(q, (ns, *chunk)))
        return out

    def scan(self, ns: str) -> Iterable[tuple[str, Any]]:
        if self.conn is None:
            return
        for k, v in self.conn.execute("SELECT key, value FROM kv WHERE ns = ?", (ns,)):
            yield k, json.loads(v)

    def raw_wikitext(self, page_title: str) -> str | None:
        """Wikitext of a page canon cached under ``raw/`` (None when absent)."""
        path = self.root / "raw" / f"wp_{tn.slug(page_title, 120)}.json"
        if not path.exists():
            return None
        return json.loads(path.read_bytes())["parse"]["wikitext"]


class WikiClient:
    """Cached, polite access to the Wikidata and Wikipedia APIs.

    ``http`` is anything with ``get(url, params=..., robots=..., use_cache=...)`` returning
    an object with ``status``, ``headers`` and ``json()`` (``musichistory.http.PoliteClient``
    by default; tests pass a fake).
    """

    def __init__(self, cache_dir: Path | None = None, http=None, *, refresh: bool = False,
                 interval: float | None = None, log: Log = print, sleep: Callable[[float], None] = time.sleep) -> None:
        self.kv = KVCache(Path(cache_dir or THEMES_CACHE) / "wikidata.sqlite")
        if http is None:
            from ..http import PoliteClient

            iv = WIKIMEDIA_INTERVAL if interval is None else interval
            http = PoliteClient(min_interval={"www.wikidata.org": iv, "en.wikipedia.org": iv},
                                timeout=90.0, max_retries=5)
            http.rate_groups.update({"www.wikidata.org": "wikimedia", "en.wikipedia.org": "wikimedia"})
        self.http = http
        self.refresh = refresh
        self.log = log
        self.sleep = sleep
        self.requests: Counter = Counter()      # API calls made, per action
        self.served: Counter = Counter()        # answers per (namespace, 'cache' | 'network')
        self.net_keys: set[tuple[str, str]] = set()  # (namespace, key) fetched in this run
        self._mem: dict[str, dict[str, Any]] = defaultdict(dict)
        self._reach: dict[tuple, bool] = {}

    # -- plumbing ---------------------------------------------------------------------
    def _call(self, api: str, params: dict) -> dict:
        params = {**params, "format": "json", "formatversion": "2"}
        if api == WIKIDATA_API:
            params["maxlag"] = "5"
        attempt = 0
        while True:
            r = self.http.get(api, params=params, robots=False, use_cache=False)
            self.requests[params.get("action", "?") if params.get("action") != "query" else
                          "query:" + (params.get("prop") or params.get("list") or "")] += 1
            if r.status != 200:
                attempt += 1
                if attempt > 5:
                    raise ApiError(f"HTTP {r.status} from {api}")
                try:
                    wait = float((r.headers or {}).get("Retry-After") or 0)
                except ValueError:
                    wait = 0.0
                self.sleep(min(120.0, max(wait, 10.0 * attempt)))
                continue
            data = r.json()
            err = data.get("error")
            if err:
                if err.get("code") == "maxlag" and attempt < 8:
                    attempt += 1
                    try:
                        wait = float((r.headers or {}).get("Retry-After") or 5)
                    except ValueError:
                        wait = 5.0
                    self.sleep(min(60.0, max(5.0, wait)))
                    continue
                raise ApiError(f"{err.get('code')}: {err.get('info')}")
            return data

    def _cached(self, ns: str, keys: list[str]) -> dict[str, Any]:
        out = {k: self._mem[ns][k] for k in keys if k in self._mem[ns]}
        rest = [k for k in keys if k not in out]
        if rest and not self.refresh:
            got = self.kv.get_many(ns, rest)
            self._mem[ns].update(got)
            out.update(got)
            self.served[(ns, "cache")] += len(got)
        return out

    def _store(self, ns: str, items: dict[str, Any]) -> None:
        self.kv.put_many(ns, items)
        self._mem[ns].update(items)
        if ns == ENT:
            self._reach.clear()  # newly fetched classes can change an answer
        self.net_keys.update((ns, k) for k in items)
        self.served[(ns, "network")] += len(items)

    # -- public -----------------------------------------------------------------------
    def entities(self, qids: Iterable[str]) -> dict[str, dict]:
        """QID -> trimmed entity ({} when missing). A redirected id maps to its target's
        data, with ``redirect_to`` set."""
        qids = [q for q in dict.fromkeys(qids) if q and re.fullmatch(r"Q\d+", q)]
        out = self._cached(ENT, qids)
        todo = [q for q in qids if q not in out]
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            data = self._call(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(batch),
                                             "props": "labels|descriptions|aliases|claims|sitelinks",
                                             "languages": "en|mul", "sitefilter": "enwiki"})
            ents = data.get("entities", {})
            got: dict[str, dict] = {}
            for q in batch:
                e = ents.get(q)
                if e is None:  # redirected: keyed by the target, with redirects.from == q
                    e = next((v for v in ents.values() if (v.get("redirects") or {}).get("from") == q), None)
                if e is None or "missing" in e:
                    got[q] = {}
                else:
                    t = trim(e)
                    if t.get("id") != q:
                        got[t["id"]] = dict(t)
                        t["redirect_to"] = t["id"]
                    got[q] = t
            self._store(ENT, got)
            out.update(got)
            if len(todo) > BATCH:
                self.log(f"    wikidata entities {min(i + BATCH, len(todo))}/{len(todo)}")
        return out

    def entity(self, qid: str | None) -> dict:
        if not qid:
            return {}
        return self.entities([qid]).get(qid, {})

    def pageprops(self, titles: Iterable[str]) -> dict[str, dict]:
        """English Wikipedia title -> {"page": resolved title, "qid": QID or None}."""
        titles = [t for t in dict.fromkeys(titles) if t and not re.search(r"[\[\]{}<>|#]", t)]
        out = self._cached("pageprops", titles)
        todo = [t for t in titles if t not in out]
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            data = self._call(WIKIPEDIA_API, {"action": "query", "titles": "|".join(batch), "prop": "pageprops",
                                              "ppprop": "wikibase_item|disambiguation", "redirects": "1"})
            q = data.get("query", {})
            norm = {n["from"]: n["to"] for n in q.get("normalized", [])}
            red = {r["from"]: r["to"] for r in q.get("redirects", [])}
            pages = {p["title"]: p for p in q.get("pages", [])}
            got = {}
            for t in batch:
                tt = norm.get(t, t)
                tt = red.get(tt, tt).split("#")[0]
                pp = pages.get(tt, {}).get("pageprops", {})
                got[t] = {"page": tt, "qid": None if "disambiguation" in pp else pp.get("wikibase_item")}
            self._store("pageprops", got)
            out.update(got)
        return out

    def search(self, name: str) -> list[dict]:
        """wbsearchentities: [{"id", "label", "desc", "match"}] in result order."""
        hit = self._cached("search", [name])
        if name in hit:
            return hit[name]
        data = self._call(WIKIDATA_API, {"action": "wbsearchentities", "search": name, "language": "en",
                                         "uselang": "en", "type": "item", "limit": "10"})
        res = [{"id": r.get("id"), "label": r.get("label", ""), "desc": r.get("description", ""),
                "match": (r.get("match") or {}).get("text", "")} for r in data.get("search", [])]
        self._store("search", {name: res})
        return res

    def members_of(self, group: str) -> list[str]:
        """Items stating "member of" (P463) the group: Wikidata search ``haswbstatement``."""
        hit = self._cached(MEMBERS, [group])
        if group in hit:
            return hit[group]
        data = self._call(WIKIDATA_API, {"action": "query", "list": "search", "srnamespace": "0",
                                         "srsearch": f"haswbstatement:P463={group}", "srlimit": "50"})
        res = [r["title"] for r in data.get("query", {}).get("search", []) if re.fullmatch(r"Q\d+", r.get("title", ""))]
        self._store(MEMBERS, {group: res})
        return res

    # -- class hierarchy ----------------------------------------------------------------
    def prefetch_classes(self, classes: Iterable[str], depth: int = CLASS_DEPTH) -> None:
        """Fetch the P279 ancestry of ``classes`` level by level (batched)."""
        frontier = set(classes)
        seen: set[str] = set()
        for _ in range(depth):
            frontier -= seen
            if not frontier:
                return
            seen |= frontier
            ents = self.entities(sorted(frontier))
            # nothing above a root class matters to any test in this module
            frontier = {p for q in frontier - _STOP_CLASSES for p in ents.get(q, {}).get("P279", [])}

    def reaches(self, classes: Iterable[str], roots: frozenset[str] | set[str], depth: int = CLASS_DEPTH) -> bool:
        """True when a class in ``classes`` is, or is a P279 descendant of, a root (only
        classes already fetched are followed: call ``prefetch_classes`` first)."""
        memo_key = (frozenset(classes), frozenset(roots), depth)
        if memo_key in self._reach:
            return self._reach[memo_key]
        self._reach[memo_key] = got = self._reaches(set(memo_key[0]), roots, depth)
        return got

    def _reaches(self, frontier: set[str], roots: frozenset[str] | set[str], depth: int) -> bool:
        seen: set[str] = set()
        for _ in range(depth + 1):
            if frontier & roots:
                return True
            seen |= frontier
            nxt = set()
            for q in frontier:
                e = self._mem[ENT].get(q)
                if e is None and not self.refresh:
                    e = self._cached(ENT, [q]).get(q)
                nxt.update((e or {}).get("P279", []))
            frontier = nxt - seen
            if not frontier:
                return False
        return False


# ---------------------------------------------------------------------------- names
_ACT_SPLIT = re.compile(r"\s+(?:featuring|feat\.?|ft\.?|with|duet with|vs\.?|x)\s+|,|/|;", re.I)
_LEADER = re.compile(r"^(?P<leader>.+?)\s+(?:&|and|\+)\s+(?:the|his|her)\s+\S", re.I)
_DUET_SPLIT = re.compile(r"\s+(?:&|and|\+)\s+", re.I)


@dataclass
class ActNames:
    whole: str                  # credit without featuring guests
    lead: str                   # its first act
    leader: str | None = None   # "Gladys Knight" of "Gladys Knight & the Pips"
    # duet partners of the first act, each as alternative names in order of preference
    parts: list[list[str]] = field(default_factory=list)


# Duet "partners" that are not acts: "Dionne Warwick & Friends", "X and Orchestra".
_GENERIC_PARTS = frozenset({"friends", "orchestra", "his orchestra", "her orchestra", "chorus", "choir",
                            "company", "band", "his band", "the band"})


def act_names(credit: str) -> ActNames:
    whole = re.sub(r"[\"“”]", "", tn.strip_featuring(credit or "")).strip(" ,;/")
    lead = _ACT_SPLIT.split(whole)[0].strip() or whole
    m = _LEADER.match(lead)
    leader = m.group("leader").strip() if m else None
    split = [] if leader else [p.strip() for p in _DUET_SPLIT.split(lead) if p.strip()]
    parts: list[list[str]] = []
    if len(split) > 1:
        # "Frank & Nancy Sinatra", "Inez and Charlie Foxx": a lone first name may share the
        # last partner's surname (tried first; "Queen and David Bowie" falls back to "Queen")
        last = split[-1].split()
        for i, p in enumerate(split):
            if tn.artist_key(p) in _GENERIC_PARTS:
                continue
            alt = [f"{p} {last[-1]}"] if i < len(split) - 1 and len(p.split()) == 1 and len(last) >= 2 else []
            parts.append(alt + [p])
    return ActNames(whole, lead, leader, parts)


def _key(name: str) -> str:
    return tn.squash(tn.artist_key(name))


_ANY_ACT_SPLIT = re.compile(r"\s+(?:&|and|\+|featuring|feat\.?|ft\.?|with|x|vs\.?)\s+|,", re.I)


def n_acts(name: str) -> int:
    return len([p for p in _ANY_ACT_SPLIT.split(name or "") if p.strip()])


def names_match(a: str, b: str, exact: bool = False) -> bool:
    """Two names of one act: canon's fuzzy/acronym artist match, in either direction, and
    the same number of joined acts ("Elton John" is not "Elton John & Kiki Dee").
    ``exact``: normalized keys must be equal."""
    if not a or not b:
        return False
    if _key(a) == _key(b):
        return True
    return not exact and n_acts(a) == n_acts(b) and (same_artist(a, b) or same_artist(b, a))


def _tokens(name: str) -> set[str]:
    return {tn.drop_g(t) for t in tn.artist_key(name).split() if t != "and"}


def contains_name(full: str, short: str) -> bool:
    """Every word of ``short`` is a word of ``full`` ("Katrina" in "Katrina Leskanich")."""
    s = _tokens(short)
    return bool(s) and s <= _tokens(full)


def guess_titles(name: str) -> list[str]:
    """Likely English Wikipedia article titles of an act, most likely first."""
    n = name.strip()
    if not n or re.search(r"[\[\]{}<>|#]", n):
        return []
    n = n[0].upper() + n[1:]
    out = [n] + [f"{n} ({d})" for d in ("band", "singer", "musician", "rapper", "group", "American band")]
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------- inputs
@dataclass
class WorkIn:
    work_id: str
    title: str
    credit: str
    qid: str | None
    names: ActNames
    song_qids: list[str] = field(default_factory=list)   # composition + recording items
    performers: list[str] = field(default_factory=list)  # their P175 values
    artist_links: list[str] = field(default_factory=list)  # articles linked from list rows' artist cells
    midi_vocal: bool | None = None                        # sung text in the MIDI candidates (None: none)
    year: int | None = None                               # year of the canonical recording


def _features_vocal(features: list[tuple[dict, bool]]) -> bool | None:
    """Sung text in a work's MIDI candidates: (features, chosen) pairs -> True / False / None."""
    if not features:
        return None
    for f, chosen in features:
        vocal_track = any(t.get("role") == "vocal" for t in f.get("tracks", []))
        lyrics = f.get("lyric_events") or 0
        if vocal_track or f.get("karaoke") or lyrics >= (1 if chosen else LYRIC_EVENTS_MIN):
            return True
    return False


def _list_artist_links(canon: CanonCache, rows: list[sqlite3.Row]) -> dict[tuple, tuple[str, ...]]:
    """(list_id, rank, raw_title, raw_artist) -> artist-cell links, from canon's cached pages."""
    from ..canon import billboard, grammy, spotify

    wanted = {r["list_id"] for r in rows}
    out: dict[tuple, tuple[str, ...]] = {}
    entries = []
    for lid in sorted(wanted):
        if lid.startswith("billboard_ye_"):
            year = int(lid.rsplit("_", 1)[1])
            text = canon.raw_wikitext(billboard.page_title(year))
            if text:
                entries += billboard.parse_page(text, year)
        elif lid == grammy.LIST_ID:
            got: list = []
            for title in grammy.PAGES:
                text = canon.raw_wikitext(title)
                if text:
                    got += grammy.parse_page(text, start_rank=len(got) + 1)
            entries += got
        elif lid == spotify.LIST_ID:
            text = canon.raw_wikitext(spotify.PAGE)
            if text:
                entries += spotify.parse_page(text)
    for e in entries:
        if e.artist_links:
            out[(e.list_id, e.rank, e.raw_title, e.raw_artist)] = tuple(e.artist_links)
    return out


def load_inputs(conn: sqlite3.Connection, work_ids: list[str], canon: CanonCache, log: Log = print) -> dict[str, WorkIn]:
    ids = list(dict.fromkeys(work_ids))
    out: dict[str, WorkIn] = {}
    marks = lambda n: ",".join("?" * n)  # noqa: E731
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(f"SELECT work_id, title, canonical_artist, wikidata_qid,"
                              f" COALESCE(effective_year, work_year) AS year FROM work"
                              f" WHERE work_id IN ({marks(len(chunk))})", chunk):
            qid = r["wikidata_qid"] or (r["work_id"] if re.fullmatch(r"Q\d+", r["work_id"]) else None)
            out[r["work_id"]] = WorkIn(r["work_id"], r["title"], r["canonical_artist"] or "", qid,
                                       act_names(r["canonical_artist"] or ""), year=r["year"])
    # MIDI evidence of sung text
    feats: dict[str, list[tuple[dict, bool]]] = defaultdict(list)
    le_rows: list[sqlite3.Row] = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(
                f"SELECT c.work_id, c.features_json, (s.candidate_id = c.candidate_id) AS chosen FROM candidate c"
                f" LEFT JOIN selection s ON s.work_id = c.work_id"
                f" WHERE c.work_id IN ({marks(len(chunk))}) AND c.features_json IS NOT NULL", chunk):
            try:
                feats[r["work_id"]].append((json.loads(r["features_json"]), bool(r["chosen"])))
            except ValueError:
                continue
        le_rows += conn.execute(f"SELECT list_id, rank, raw_title, raw_artist, wiki_link, work_id FROM list_entry"
                                f" WHERE work_id IN ({marks(len(chunk))})", chunk).fetchall()
    for wid, w in out.items():
        w.midi_vocal = _features_vocal(feats.get(wid, []))
    # the work's own Wikidata items (canon cache): composition, P2550 recordings, linked song articles
    rec_of: dict[str, list[str]] = defaultdict(list)
    wd_cache: dict[str, dict] = {}
    targets = {w.qid for w in out.values() if w.qid}
    for k, e in canon.scan("wd"):
        wd_cache[k] = e
        for t in e.get("P2550", []):
            if t in targets:
                rec_of[t].append(k)
    pp = canon.get_many("pageprops", [r["wiki_link"] for r in le_rows if r["wiki_link"]])
    links = _list_artist_links(canon, [r for r in le_rows if r["list_id"].startswith(("billboard", "grammy", "spotify"))])
    by_work: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in le_rows:
        by_work[r["work_id"]].append(r)
    for wid, w in out.items():
        items = [w.qid] if w.qid else []
        items += rec_of.get(w.qid or "", [])
        for r in by_work.get(wid, []):
            q = (pp.get(r["wiki_link"]) or {}).get("qid") if r["wiki_link"] else None
            if q:
                items.append(wd_cache.get(q, {}).get("redirect_to", q))
        w.song_qids = list(dict.fromkeys(items))
        w.performers = list(dict.fromkeys(p for q in w.song_qids for p in wd_cache.get(q, {}).get("P175", [])))
        w.artist_links = list(dict.fromkeys(
            t for r in by_work.get(wid, []) for t in links.get((r["list_id"], r["rank"], r["raw_title"], r["raw_artist"]), ())))
    return out


# ---------------------------------------------------------------------------- resolver
@dataclass
class Hit:
    qid: str
    how: str                    # 'p175' | 'links' | 'wikipedia' | 'search'
    keys: tuple = ()            # cache keys that produced it (for cache/network accounting)


class Resolver:
    def __init__(self, client: WikiClient, canon: CanonCache, log: Log = print) -> None:
        self.c = client
        self.canon = canon
        self.log = log
        self.labels = {}  # performer QID -> label (canon cache)
        self.extra_members: dict[str, list[str]] = {}  # group QID -> P463 members

    # -- entity helpers ------------------------------------------------------------------
    def ent(self, qid: str | None) -> dict:
        return self.c.entity(qid) if qid else {}

    def canonical(self, qid: str) -> str:
        return self.ent(qid).get("redirect_to", qid)

    def is_human(self, e: dict) -> bool:
        return HUMAN in e.get("P31", ())

    def is_group(self, e: dict) -> bool:
        return not self.is_human(e) and self.c.reaches(e.get("P31", ()), ENSEMBLE_ROOTS)

    def is_musician(self, e: dict) -> bool:
        return self.c.reaches(e.get("P106", ()), MUSIC_ROOTS) or VOICE in e.get("P1303", ())

    def sings(self, e: dict) -> bool:
        return self.c.reaches(e.get("P106", ()), VOCAL_ROOTS) or VOICE in e.get("P1303", ())

    def is_act(self, e: dict, strict: bool) -> bool:
        """A performer item: a human (strict: with a musical occupation) or an ensemble."""
        if not e:
            return False
        if self.is_human(e):
            return self.is_musician(e) or not strict
        return self.is_group(e)

    def prepare(self, qids: Iterable[str]) -> dict[str, dict]:
        """Fetch entities and the class ancestry their type checks need."""
        ents = self.c.entities(qids)
        targets = {e["redirect_to"] for e in ents.values() if e.get("redirect_to")} - set(ents)
        if targets:
            ents.update(self.c.entities(targets))
        classes = {c for e in ents.values() for p in ("P31", "P106") for c in e.get(p, ())}
        self.c.prefetch_classes(classes)
        return ents

    def prepare_members(self, act_qids: Iterable[str]) -> None:
        """Fetch the members of every group among ``act_qids``: its P527 values (and their
        role classes); for a group where none of those is a singing human, also the items
        that say they are a member of it (P463), found by a Wikidata search."""
        groups = [e for e in (self.ent(q) for q in sorted(set(act_qids))) if self.is_group(e)]
        self.prepare({m["id"] for g in groups for m in g.get("P527", [])})
        self.c.prefetch_classes({x for g in groups for m in g.get("P527", []) for x in m.get("roles", [])})
        todo = [g["id"] for g in groups if g["id"] not in self.extra_members
                and not any(self.sings(e) for e, _ in self.members(g))]
        if todo:
            self.log(f"  singer: looking up members (P463) of {len(todo)} groups")
        for i, gid in enumerate(todo):
            self.extra_members[gid] = self.c.members_of(gid)
            if i % 50 == 49:
                self.log(f"    member searches {i + 1}/{len(todo)}")
        self.prepare({q for gid in todo for q in self.extra_members[gid]})

    # -- act resolution -------------------------------------------------------------------
    def resolve(self, reqs: list[tuple[WorkIn, str, str, Any]]) -> dict[tuple[str, str], Hit]:
        """(work, name, mode, group) -> Hit, through p175, list links, article guesses and
        search.

        ``mode``: "fuzzy" (``names_match``), "exact" (equal normalized names: for a name we
        built, like "Frank Sinatra" from "Frank & Nancy Sinatra"), or "subset" (fuzzy, and
        else the only performer of the work whose label contains every word of the name:
        "Janet" -> "Janet Jackson", "Stones" -> "The Rolling Stones"). ``group``: names that
        stand for one act (a credit and its first act, a partner's alternative spellings);
        a name is only searched when no name of its group was found without searching.
        """
        hits: dict[tuple[str, str], Hit] = {}
        todo = [(w, n, sub, g) for w, n, sub, g in reqs if n]
        # 1. P175 of the work's items (canon cache)
        perf = {p for w, *_ in todo for p in w.performers}
        self.labels.update(self.canon.get_many("wd_label", perf))
        unlabeled = [p for p in perf if not self.labels.get(p)]
        if unlabeled:
            for q, e in self.c.entities(unlabeled).items():
                self.labels[q] = e.get("label", "")
        cand: dict[tuple[str, str], str] = {}
        for w, n, mode, _ in todo:
            named = [p for p in w.performers if names_match(self.labels.get(p, ""), n, mode == "exact")]
            if not named and mode == "subset":
                named = [p for p in w.performers if contains_name(self.labels.get(p, ""), n)]
                named = named if len(named) == 1 else []
            if named:
                cand[(w.work_id, n)] = named[0]
        self.prepare(cand.values())
        for key, q in cand.items():
            q = self.canonical(q)
            if self.is_act(self.ent(q), strict=False):
                hits[key] = Hit(q, "p175", ((ENT, q),))
        todo = [t for t in todo if (t[0].work_id, t[1]) not in hits]
        # 2. artist-cell links of the work's list rows (the first linked article that is an act)
        link_for: dict[tuple[str, str], list[str]] = {}
        for w, n, mode, _ in todo:
            ts = [t for t in w.artist_links if names_match(split_disamb(t)[0], n, mode == "exact")]
            if ts:
                link_for[(w.work_id, n)] = ts
        for key, found_acts in self._title_candidates(link_for, strict=False).items():
            q, t = found_acts[0]
            hits[key] = Hit(q, "links", (("pageprops", t), (ENT, q)))
        todo = [t for t in todo if (t[0].work_id, t[1]) not in hits]
        # 3. article title guesses (name-level, shared across works); several different acts
        #    behind the guesses ("X (band)" and "X (singer)") are settled by the search ranking
        names = list(dict.fromkeys(n for _, n, *_ in todo))
        guessed = self._title_candidates({n: guess_titles(n) for n in names}, strict=True)
        ambiguous = {n: c for n, c in guessed.items() if len(c) > 1}
        for w, n, *_ in todo:
            if n in guessed and n not in ambiguous:
                q, t = guessed[n][0]
                hits[(w.work_id, n)] = Hit(q, "wikipedia", (("pageprops", t), (ENT, q)))
        found_groups = {g for w, n, _, g in reqs if (w.work_id, n) in hits}
        todo = [t for t in todo if (t[0].work_id, t[1]) not in hits and t[3] not in found_groups]
        # 4. wbsearchentities
        names = list(dict.fromkeys(n for _, n, *_ in todo))
        if names:
            self.log(f"  singer: searching Wikidata for {len(names)} act names")
        exact = {n for _, n, mode, _ in todo if mode == "exact"}
        found: dict[str, list[dict]] = {}
        for i, n in enumerate(names):
            amb = {q for q, _ in ambiguous.get(n, [])}
            found[n] = [r for r in self.c.search(n) if r.get("id") and (
                r["id"] in amb or names_match(r.get("match", ""), n, n in exact)
                or names_match(r.get("label", ""), n, n in exact))]
            if i % 50 == 49:
                self.log(f"    searched {i + 1}/{len(names)}")
        self.prepare({r["id"] for rs in found.values() for r in rs})
        for w, n, *_ in todo:
            key = (w.work_id, n)
            if n in ambiguous:
                amb = {q: t for q, t in ambiguous[n]}
                pick = next((self.canonical(r["id"]) for r in found.get(n, []) if self.canonical(r["id"]) in amb), None)
                q = pick or ambiguous[n][0][0]
                hits[key] = Hit(q, "wikipedia", (("pageprops", amb[q]), (ENT, q)) + ((("search", n),) if pick else ()))
                continue
            for r in found.get(n, []):
                q = self.canonical(r["id"])
                e = self.ent(q)
                texts = [r.get("match", ""), r.get("label", ""), e.get("label", "")] + e.get("aliases", [])
                if any(names_match(t, n, n in exact) for t in texts if t) and self.is_act(e, strict=True):
                    hits[key] = Hit(q, "search", (("search", n), (ENT, q)))
                    break
        return hits

    def _title_candidates(self, titles: dict[Any, list[str]], strict: bool) -> dict[Any, list[tuple[str, str]]]:
        """key -> [(QID, title)]: the distinct acts behind ``titles``, in title order."""
        all_titles = [t for ts in titles.values() for t in ts]
        known = self.canon.get_many("pageprops", all_titles)
        pp = {t: v for t, v in known.items() if v.get("qid")}
        pp.update(self.c.pageprops([t for t in all_titles if t not in pp]))
        self.prepare({v["qid"] for v in pp.values() if v.get("qid")})
        out: dict[Any, list[tuple[str, str]]] = {}
        for key, ts in titles.items():
            acts: dict[str, str] = {}
            for t in ts:
                q = (pp.get(t) or {}).get("qid")
                if q:
                    q = self.canonical(q)
                    if q not in acts and self.is_act(self.ent(q), strict=strict):
                        acts[q] = t
            if acts:
                out[key] = list(acts.items())
        return out

    # -- gender -----------------------------------------------------------------------------
    @staticmethod
    def person_gender(e: dict) -> str:
        vals = e.get("P21") or []
        pref = [q for q, r in vals if r == "preferred"] or [q for q, _ in vals]
        got = {"male" if q in MALE_IDS else "female" if q in FEMALE_IDS else "nonbinary" for q in pref}
        if not got:
            return "unknown"
        if len(got) == 1:
            return got.pop()
        return "nonbinary" if "nonbinary" in got else "unknown"

    @staticmethod
    def combine(genders: Iterable[str]) -> str:
        known = {g for g in genders if g not in ("unknown", "instrumental")}
        if not known:
            return "unknown"
        return known.pop() if len(known) == 1 else "mixed"

    def members(self, group: dict, year: int | None = None) -> list[tuple[dict, list[str]]]:
        """(member entity, P527 role qualifiers) for the group's human members (P527, plus
        the P463 members found by ``prepare_members``). With ``year``: only the members whose
        membership span (P580/P582 on either statement) covers it — all of them when the
        spans leave nobody."""
        gid = group.get("id", "")
        out: dict[str, tuple[dict, list[str], list | None]] = {}
        refs = [(m["id"], m.get("roles", []), m.get("span")) for m in group.get("P527", [])]
        refs += [(q, [], None) for q in self.extra_members.get(gid, [])]
        for q, roles, span in refs:
            e = self.ent(self.canonical(q))
            if self.is_human(e) and e["id"] not in out:
                out[e["id"]] = (e, roles, span or e.get("P463_span", {}).get(gid))
        every = [(e, roles) for e, roles, _ in out.values()]
        if year is None:
            return every
        return [(e, roles) for e, roles, span in out.values() if active(span, year)] or every

    def member_keys(self, group: dict) -> set[tuple[str, str]]:
        """Cache keys the group's member list depends on (for cache/network accounting)."""
        keys = {(ENT, e["id"]) for e, _ in self.members(group)}
        if group.get("id") in self.extra_members:
            keys.add((MEMBERS, group["id"]))
        return keys

    def group_gender(self, group: dict, year: int | None = None) -> tuple[str, str]:
        humans = self.members(group, year)
        # P527 role qualifiers: "lead vocalist" (or a singing "front person") first, then
        # any vocal role, then singing occupations, then everyone
        lead = [e for e, roles in humans if LEAD_VOCALIST in roles or (FRONT_PERSON in roles and self.sings(e))]
        voiced = [e for e, roles in humans if roles and self.c.reaches(roles, VOCAL_ROOTS)]
        singers = [e for e, _ in humans if self.sings(e)]
        for pool, how in ((lead, "group-lead"), (voiced, "group-vocal-role"), (singers, "group-singers"),
                          ([e for e, _ in humans], "group-members")):
            if pool:
                g = self.combine(self.person_gender(e) for e in pool)
                if g != "unknown":
                    return g, how
        classes = set(group.get("P31", ()))
        if GIRL_GROUP in classes or self.c.reaches(classes, {GIRL_GROUP}, depth=1):
            return "female", "group-class"
        if BOY_BAND in classes or self.c.reaches(classes, {BOY_BAND}, depth=1):
            return "male", "group-class"
        return "unknown", "group-no-members"

    def leader_member(self, group: dict, leader: str) -> dict | None:
        for e, _ in self.members(group):
            if any(contains_name(t, leader) for t in [e.get("label", "")] + e.get("aliases", []) if t):
                return e
        return None

    def act_gender(self, e: dict, year: int | None = None) -> tuple[str, str]:
        if self.is_human(e):
            return self.person_gender(e), "person"
        if self.is_group(e):
            return self.group_gender(e, year)
        return "unknown", "not-an-act"

    # -- instrumental -----------------------------------------------------------------------
    def wd_instrumental(self, w: WorkIn, acts: list[dict]) -> str | None:
        """Which Wikidata statement calls the work (or its act) instrumental, if any."""
        songs = [self.ent(self.canonical(q)) for q in w.song_qids]
        if any(VOCAL_TRACK in s.get("P31", ()) for s in songs):
            return None
        for s in songs:
            if INSTRUMENTAL_TRACK_TYPES & set(s.get("P31", ())):
                return "song-type"
            if INSTRUMENTAL_GENRES & set(s.get("P136", ())):
                return "song-genre"
            if _INSTRUMENTAL_DESC.search(s.get("desc", "")):
                return "song-description"
            if re.search(r"\(instrumental\)\s*$", s.get("enwiki", ""), re.I):
                return "song-article"
        for a in acts:
            if self.c.reaches(a.get("P31", ()), {INSTRUMENTAL_ENSEMBLE}):
                return "act-type"
            if INSTRUMENTAL_GENRES & set(a.get("P136", ())):
                return "act-genre"
        # an act nobody in which sings (a bandleader, a guitar combo), for a work no item of
        # which is a song (typed Q7366 "composition for voices", described as a "song") or
        # credits a lyricist (P676)
        if acts and songs and all(self.non_singing(a, w.year) for a in acts) and not any(
                SONG in s.get("P31", ()) or s.get("P676") or re.search(r"\bsong\b", s.get("desc", ""), re.I)
                for s in songs):
            return "act-non-singing"
        return None

    def non_singing(self, act: dict, year: int | None = None) -> bool:
        """Wikidata lists what the act does and none of it is singing."""
        if self.is_human(act):
            return bool(act.get("P106")) and not self.sings(act)
        humans = [e for e, _ in self.members(act, year)]
        return bool(humans) and not any(self.sings(e) for e in humans)


def _decide(r: Resolver, w: WorkIn, hits: dict[tuple[str, str], Hit]) -> tuple[dict, set]:
    """The singer row for one work, and the cache keys its answer depends on."""
    keys: set = set()
    nm = w.names
    row = {"gender": "unknown", "source": "unresolved", "artist_qid": None, "artist_label": None}
    acts: list[dict] = []

    def use(h: Hit) -> dict:
        keys.update(h.keys)
        e = r.ent(h.qid)
        acts.append(e)
        return e

    done = False
    for name in dict.fromkeys([nm.whole, nm.lead]):
        h = hits.get((w.work_id, name))
        if not h:
            continue
        e = use(h)
        row.update(artist_qid=h.qid, artist_label=e.get("label"))
        if r.is_group(e) and nm.leader:
            m = r.leader_member(e, nm.leader)
            if m is None:
                lh = hits.get((w.work_id, nm.leader))
                le = r.ent(lh.qid) if lh else {}
                if le and r.is_human(le) and (e.get("id") in le.get("P463", ()) or not r.members(e)):
                    keys.update(lh.keys)
                    m = le
            if m is not None and (r.sings(m) or not r.members(e)):
                keys.add((ENT, m.get("id")))
                g = r.person_gender(m)
                if g != "unknown":
                    row.update(gender=g, source=f"{h.how}:leader")
                    done = True
                    break
        g, how = r.act_gender(e, w.year)
        keys.update(r.member_keys(e))
        row.update(gender=g, source=f"{h.how}:{how}")
        done = True
        break
    if not done and nm.leader:
        h = hits.get((w.work_id, nm.leader))
        if h:
            e = use(h)
            if r.is_human(e):
                row.update(gender=r.person_gender(e), source=f"{h.how}:leader",
                           artist_qid=h.qid, artist_label=e.get("label"))
                done = True
    if not done and nm.parts:
        found = [next((hits[(w.work_id, a)] for a in alts if (w.work_id, a) in hits), None) for alts in nm.parts]
        seen_q: set[str] = set()
        got = []
        for h in found:  # two partners found as one item is one partner found
            if h and h.qid not in seen_q:
                seen_q.add(h.qid)
                got.append((h, use(h)))
        if got:
            singing = [(h, e) for h, e in got if r.is_group(e) or r.sings(e)]
            pool = singing or got
            genders = []
            for h, e in pool:
                genders.append(r.act_gender(e, w.year)[0])
                keys.update(r.member_keys(e))
            hows = "+".join(sorted({h.how for h, _ in got}))
            partial = ":partial" if len(got) < len(found) else ""
            row.update(gender=r.combine(genders), source=f"{hows}:duet{partial}",
                       artist_qid=";".join(h.qid for h, _ in got),
                       artist_label=" & ".join(e.get("label", "") for _, e in got))
    # instrumental: no sung text in the MIDI candidates AND Wikidata says so
    if w.midi_vocal is False:
        keys.update((ENT, q) for q in w.song_qids)
        why = r.wd_instrumental(w, acts)
        if why:
            row.update(gender="instrumental", source=f"{row['source'].split(':')[0]}:instrumental-{why}")
    return row, keys


# ---------------------------------------------------------------------------- public API
def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def resolve_singers(conn: sqlite3.Connection, work_ids: Iterable[str], *, refresh: bool = False,
                    client: WikiClient | None = None, canon_cache: Path | None = None,
                    log: Log = print, stats: dict | None = None) -> dict[str, dict]:
    """Resolve the lead singer's gender of ``work_ids`` and write the ``singer`` table.

    Rows are recomputed on every call from the cached Wikidata answers (only what is not
    cached is fetched); ``refresh=True`` re-fetches the answers this run needs.
    """
    ensure_table(conn)
    ids = list(dict.fromkeys(work_ids))
    client = client or WikiClient(refresh=refresh, log=log)
    canon = CanonCache(canon_cache)
    inputs = load_inputs(conn, ids, canon, log)
    r = Resolver(client, canon, log)
    log(f"  singer: {len(inputs)} works; resolving lead acts")
    first = [(w, n, "subset", w.work_id) for w in inputs.values() for n in dict.fromkeys([w.names.whole, w.names.lead])]
    hits = r.resolve(first)
    r.prepare_members(h.qid for h in hits.values())
    # second round: leaders and duet partners where the credit itself did not settle it
    second = []
    for w in inputs.values():
        top = hits.get((w.work_id, w.names.whole)) or hits.get((w.work_id, w.names.lead))
        e = r.ent(top.qid) if top else {}
        if w.names.leader and (not e or (r.is_group(e) and r.leader_member(e, w.names.leader) is None)):
            second.append((w, w.names.leader, "fuzzy", (w.work_id, "leader")))
        if not top:  # a partner's surname-expanded name ("Frank Sinatra") must match exactly
            second += [(w, a, "exact" if len(alts) > 1 and j == 0 else "fuzzy", (w.work_id, i))
                       for i, alts in enumerate(w.names.parts) for j, a in enumerate(alts)]
    if second:
        hits.update(r.resolve(second))
        r.prepare_members(h.qid for h in hits.values())
    # the song items of works whose MIDI shows no vocals
    r.prepare({q for w in inputs.values() if w.midi_vocal is False for q in w.song_qids})

    out: dict[str, dict] = {}
    net = cache_only = 0
    how = Counter()
    for wid in ids:
        w = inputs.get(wid)
        if w is None:
            continue
        row, keys = _decide(r, w, hits)
        out[wid] = row
        how[row["source"].split(":")[0]] += 1
        if keys & client.net_keys:
            net += 1
        else:
            cache_only += 1
    conn.executemany(
        "INSERT OR REPLACE INTO singer(work_id, gender, source, artist_qid, artist_label) VALUES (?,?,?,?,?)",
        [(wid, v["gender"], v["source"], v["artist_qid"], v["artist_label"]) for wid, v in out.items()])
    conn.commit()
    dist = Counter(v["gender"] for v in out.values())
    log(f"  singer: {dict(dist)}")
    log(f"  singer: act found via {dict(how)}; answers from cache only: {cache_only}, needed network: {net};"
        f" API requests {dict(client.requests)}")
    if stats is not None:
        stats.update(distribution=dict(dist), act_source=dict(how), cache_only=cache_only, network=net,
                     requests=dict(client.requests), served={f"{k[0]}:{k[1]}": v for k, v in client.served.items()})
    return out


def selected_work_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT work_id FROM work WHERE selected >= 1 ORDER BY canon_rank, work_id")]


def main(argv: list[str] | None = None) -> int:
    import sys

    from .. import db

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m musichistory.themes.singer",
                                 description="Resolve lead-singer genders of the selected works (table singer).")
    ap.add_argument("--work", action="append", help="only this work_id (repeatable)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch Wikidata answers instead of the cache")
    args = ap.parse_args(argv)
    conn = db.connect()
    ids = args.work or selected_work_ids(conn)
    t0 = time.monotonic()
    resolve_singers(conn, ids, refresh=args.refresh)
    print(f"  singer: done in {time.monotonic() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
