"""Singer gender resolution (musichistory.themes.singer) against a fake Wikimedia API.

Every act, song and item here is invented; no network is used.
"""

from __future__ import annotations

import json

import pytest

from musichistory import db
from musichistory.canon import billboard
from musichistory.canon.net import KVCache
from musichistory.http import Fetched
from musichistory.textnorm import slug
from musichistory.themes import singer

# ---------------------------------------------------------------------------- fake Wikidata
MALE, FEMALE, NONBINARY, TRANS_WOMAN = "Q6581097", "Q6581072", "Q48270", "Q1052281"
HUMAN, GROUP, ENSEMBLE, ROCK_BAND, GIRL_GROUP = "Q5", "Q215380", "Q2088357", "Q5741069", "Q641066"
SINGER, MUSICIAN, INSTRUMENTALIST, GUITARIST, ACTOR = "Q177220", "Q639669", "Q1278335", "Q855091", "Q33999"
VOCALIST, LEAD_VOCALIST = "Q2643890", "Q1045845"
SONG, NO_LYRICS_TRACK, FILM, MUSICAL_WORK = "Q7366", "Q55850643", "Q11424", "Q105543609"


def entity(qid, label, *, P31=(), P279=(), P21=(), P106=(), P527=(), P136=(), P463=(), desc=None,
           aliases=(), enwiki=None, preferred=(), label_lang="en"):
    def st(p, v, rank="normal"):
        return {"mainsnak": {"snaktype": "value", "property": p, "datavalue": {"value": {"id": v}}},
                "rank": rank}
    claims = {}
    for p, vals in (("P31", P31), ("P279", P279), ("P106", P106), ("P136", P136), ("P463", P463)):
        if vals:
            claims[p] = [st(p, v) for v in vals]
    if P21:
        claims["P21"] = [st("P21", v, "preferred" if v in preferred else "normal") for v in P21]
    if P527:  # a member is a QID or (QID, [role QIDs], (start year, end year) or None)
        claims["P527"] = []
        for m in P527:
            q, roles, span = (m, [], None) if isinstance(m, str) else (tuple(m) + (None,))[:3]
            s = st("P527", q)
            quals = {}
            if roles:
                quals["P3831"] = [{"snaktype": "value", "datavalue": {"value": {"id": r}}} for r in roles]
            for p, y in zip(("P580", "P582"), span or ()):
                if y:
                    quals[p] = [{"snaktype": "value", "datavalue": {"value": {"time": f"+{y}-01-01T00:00:00Z"}}}]
            if quals:
                s["qualifiers"] = quals
            claims["P527"].append(s)
    e = {"id": qid, "labels": {label_lang: {"language": label_lang, "value": label}}, "claims": claims,
         "aliases": {"en": [{"language": "en", "value": a} for a in aliases]}}
    if desc:
        e["descriptions"] = {"en": {"language": "en", "value": desc}}
    if enwiki:
        e["sitelinks"] = {"enwiki": {"site": "enwiki", "title": enwiki}}
    return e


def classes():
    return [
        entity(HUMAN, "human"), entity(ENSEMBLE, "musical ensemble"),
        entity(GROUP, "musical group", P279=[ENSEMBLE]), entity(ROCK_BAND, "rock band", P279=[GROUP]),
        entity(GIRL_GROUP, "girl group", P279=[GROUP]),
        entity(MUSICIAN, "musician"), entity(SINGER, "singer", P279=[MUSICIAN]),
        entity(INSTRUMENTALIST, "instrumentalist", P279=[MUSICIAN]),
        entity(GUITARIST, "guitarist", P279=[INSTRUMENTALIST]), entity(ACTOR, "actor"),
        entity(VOCALIST, "vocalist", P279=[MUSICIAN, SINGER]),
        entity(LEAD_VOCALIST, "lead vocalist", P279=[VOCALIST]), entity(MUSICAL_WORK, "musical work"),
        entity(SONG, "song"), entity(NO_LYRICS_TRACK, "music track without lyrics"), entity(FILM, "film"),
    ]


def world():
    """Invented acts and songs."""
    people = [
        entity("Q200", "Anna Artist", P31=[HUMAN], P21=[FEMALE], P106=[SINGER]),
        entity("Q201", "Main Guy", P31=[HUMAN], P21=[MALE], P106=[SINGER]),
        entity("Q202", "Guest Gal", P31=[HUMAN], P21=[FEMALE], P106=[SINGER]),
        entity("Q203", "Bob Tester", P31=[HUMAN], P21=[MALE], P106=[SINGER]),
        entity("Q204", "Carol Tester", P31=[HUMAN], P21=[FEMALE], P106=[SINGER]),
        entity("Q205", "Les Strummer", P31=[HUMAN], P21=[MALE], P106=[GUITARIST]),
        entity("Q206", "Mary Voice", P31=[HUMAN], P21=[FEMALE], P106=[SINGER]),
        entity("Q207", "Sky Person", P31=[HUMAN], P21=[NONBINARY, MALE], P106=[SINGER], preferred=[NONBINARY]),
        entity("Q208", "Tara Tester", P31=[HUMAN], P21=[TRANS_WOMAN], P106=[SINGER]),
        entity("Q209", "Zed Singer", P31=[HUMAN], P21=[MALE], P106=[SINGER]),
        entity("Q210", "Otto Organ", P31=[HUMAN], P21=[MALE], P106=[INSTRUMENTALIST]),
        entity("Q211", "Frank Crooner", P31=[HUMAN], P21=[MALE], P106=[SINGER]),
        entity("Q212", "Nancy Crooner", P31=[HUMAN], P21=[FEMALE], P106=[SINGER]),
        # band members
        entity("Q301", "Fiona Front", P31=[HUMAN], P21=[FEMALE], P106=[SINGER, GUITARIST]),
        entity("Q302", "Gary Guitar", P31=[HUMAN], P21=[MALE], P106=[GUITARIST]),
        entity("Q303", "Dan Drums", P31=[HUMAN], P21=[MALE], P106=[MUSICIAN]),
        entity("Q304", "Mick Mic", P31=[HUMAN], P21=[MALE], P106=[SINGER]),
        entity("Q305", "Gina Lead", P31=[HUMAN], P21=[FEMALE], P106=[SINGER]),
        entity("Q306", "Barry Backup", P31=[HUMAN], P21=[MALE], P106=[SINGER]),
        entity("Q307", "Hana Hidden", P31=[HUMAN], P21=[FEMALE], P106=[SINGER], P463=["Q405"]),
        entity("Q308", "Hal Hidden", P31=[HUMAN], P21=[MALE], P106=[GUITARIST], P463=["Q405"]),
        entity("Q309", "Rita Role", P31=[HUMAN], P21=[FEMALE], P106=[SINGER]),
        entity("Q310", "Ron Role", P31=[HUMAN], P21=[MALE], P106=[SINGER]),
        entity("Q311", "Mona Mul", P31=[HUMAN], P21=[FEMALE], P106=[SINGER], label_lang="mul"),
        entity("Q213", "Nelly", P31=[HUMAN], P21=[MALE], P106=[SINGER]),
        entity("Q214", "Kelly Tester", P31=[HUMAN], P21=[FEMALE], P106=[SINGER]),
        entity("Q215", "Merry Guest", P31=[HUMAN], P21=[FEMALE], P106=[SINGER]),
        # decoys
        entity("Q900", "Zed Singer", P31=[FILM], desc="invented film"),
        entity("Q901", "The Testers", P31=[FILM]),
    ]
    groups = [
        entity("Q400", "The Testers", P31=[ROCK_BAND], P527=["Q301", "Q302", "Q303"]),         # female lead
        entity("Q401", "Duo Mix", P31=[GROUP], P527=["Q301", "Q304"]),                         # mixed singers
        entity("Q402", "Quiet Band", P31=[GROUP], P527=["Q302", "Q303"]),                      # nobody sings
        entity("Q403", "The Glitters", P31=[GIRL_GROUP]),                                      # no members
        entity("Q404", "Gina Lead and the Backups", P31=[GROUP], P527=["Q305", "Q306"]),
        entity("Q405", "Hidden Members", P31=[GROUP]),                                  # members only via P463
        entity("Q406", "Role Band", P31=[GROUP], P527=[("Q309", [LEAD_VOCALIST]), ("Q310", [VOCALIST])]),
        entity("Q407", "The Rolling Stoners", P31=[ROCK_BAND], P527=["Q304"]),
        entity("Q408", "Era Band", P31=[GROUP], P527=[("Q309", [], (1990, 2000)), ("Q310", [], (2024, None))]),
    ]
    songs = [
        entity("Q100", "Song A", P31=[SONG]),
        entity("Q110", "Tune Without Words", P31=[NO_LYRICS_TRACK]),
        entity("Q111", "Plain Tune", P31=[SONG]),
        entity("Q112", "Wordy Tune", P31=[NO_LYRICS_TRACK]),
        entity("Q113", "Described Tune", P31=[SONG], desc="1960 instrumental by Otto Organ"),
        entity("Q114", "Duet Two", P31=[SONG]),
        entity("Q115", "Organ Tune", P31=[MUSICAL_WORK]),
        entity("Q116", "Nelly Duet", P31=[SONG]),
        entity("Q117", "Stoned", P31=[SONG]),
        entity("Q118", "Crooned Tune", P31=[MUSICAL_WORK], desc="1946 song by Otto Organ"),
    ]
    return {e["id"]: e for e in classes() + people + groups + songs}


PAGES = {  # English Wikipedia title -> QID
    "The Testers": "Q901",  # a disambiguation-like decoy: not an act
    "The Testers (band)": "Q400",
    "Duo Mix": "Q401", "Quiet Band": "Q402", "The Glitters": "Q403",
    "Main Guy": "Q201", "Bob Tester": "Q203", "Carol Tester": "Q204", "Les Strummer": "Q205",
    "Mary Voice": "Q206", "Sky Person": "Q207", "Tara Tester": "Q208", "Otto Organ": "Q210",
    "Frank Crooner": "Q211", "Nancy Crooner": "Q212",
    "Gina Lead and the Backups": "Q404", "Hidden Members": "Q405", "Role Band": "Q406", "Mona Mul": "Q311",
    "Era Band": "Q408",
}
SEARCH = {"Zed Singer": ["Q900", "Q209"]}


class FakeWikimedia:
    def __init__(self, entities=None, pages=None, search=None, maxlag_first=False):
        self.entities = entities or world()
        self.pages = PAGES if pages is None else pages
        self.search = SEARCH if search is None else search
        self.calls: list[dict] = []
        self.maxlag_pending = maxlag_first

    def _ok(self, url, data):
        return Fetched(url, 200, json.dumps(data).encode(), "application/json", {})

    def get(self, url, *, params=None, robots=True, use_cache=True, headers=None, **_):
        params = dict(params or {})
        self.calls.append({"url": url, **params})
        assert robots is False and use_cache is False
        if self.maxlag_pending and "wikidata" in url:
            self.maxlag_pending = False
            return Fetched(url, 200, json.dumps({"error": {"code": "maxlag", "info": "lagged"}}).encode(),
                           "application/json", {"Retry-After": "1"})
        action = params.get("action")
        if action == "wbgetentities":
            ids = params["ids"].split("|")
            return self._ok(url, {"entities": {q: self.entities.get(q, {"id": q, "missing": ""}) for q in ids}})
        if action == "wbsearchentities":
            res = [{"id": q, "label": self.entities[q]["labels"]["en"]["value"],
                    "match": {"type": "label", "text": params["search"]}} for q in self.search.get(params["search"], [])]
            return self._ok(url, {"search": res})
        if action == "query" and params.get("list") == "search":  # haswbstatement:P463=<group>
            group = params["srsearch"].split("=", 1)[1]
            hits = [{"ns": 0, "title": q} for q, e in self.entities.items()
                    if any(c["mainsnak"]["datavalue"]["value"]["id"] == group for c in e["claims"].get("P463", []))]
            return self._ok(url, {"query": {"search": hits}})
        if action == "query":
            titles = params["titles"].split("|")
            pages = [{"title": t, "pageprops": {"wikibase_item": self.pages[t]}} if t in self.pages
                     else {"title": t, "missing": True} for t in titles]
            return self._ok(url, {"query": {"pages": pages}})
        raise AssertionError(f"unexpected call {params}")


class Offline:
    def get(self, *a, **k):
        raise AssertionError("network used although everything is cached")


# ---------------------------------------------------------------------------- pipeline fixture
def features(lyric_events=0, vocal_track=False, karaoke=False):
    return json.dumps({"lyric_events": lyric_events, "karaoke": karaoke,
                       "tracks": [{"index": 1, "role": "vocal" if vocal_track else "melody"}]})


WORKS = [  # work_id, title, credit, song item, features of the chosen candidate
    ("W01", "Song A", "Anna Artist", "Q100", features(40)),
    ("W02", "Band Song", "The Testers", None, features(40)),
    ("W03", "Mixed Song", "Duo Mix", None, features(40)),
    ("W04", "Quiet Song", "Quiet Band", None, features(40)),
    ("W05", "Glitter Song", "The Glitters", None, features(40)),
    ("W06", "Guest Song", "Main Guy featuring Guest Gal", None, features(40)),
    ("W07", "Duet Song", "Bob Tester & Carol Tester", None, features(40)),
    ("W08", "Strum Song", "Les Strummer and Mary Voice", None, features(40)),
    ("W09", "Sky Song", "Sky Person", None, features(40)),
    ("W10", "Tara Song", "Tara Tester", None, features(40)),
    ("W11", "Found Song", "Zed Singer", None, features(40)),
    ("W12", "Leader Song", "Gina Lead & the Backups", None, features(40)),
    ("W13", "Tune Without Words", "Otto Organ", "Q110", features(0)),
    ("W14", "Plain Tune", "Otto Organ", "Q111", features(0)),
    ("W15", "Wordy Tune", "Otto Organ", "Q112", features(30)),
    ("W16", "Described Tune", "Otto Organ", "Q113", features(0)),
    ("W17", "Nobody Song", "Nobody Known", None, features(40)),
    ("W18", "Crooner Duet", "Frank & Nancy Crooner", None, features(40)),
    ("W19", "Hidden Song", "Hidden Members", None, features(40)),
    ("W20", "Role Song", "Role Band", None, features(40)),
    ("W21", "Mul Song", "Mona Mul", None, features(40)),
    ("W22", "Duet Two", "Bob Tester & Carol Tester", "Q114", features(40)),
    ("W23", "Organ Tune", "Otto Organ", "Q115", features(0)),
    ("W24", "Nelly Duet", "Nelly & Kelly Tester", "Q116", features(40)),
    ("W25", "Stoned", "Stoners", "Q117", features(40)),
    ("W26", "Era Early", "Era Band", None, features(40)),
    ("W27", "Era Late", "Era Band", None, features(40)),
    ("W28", "Crooned Tune", "Otto Organ", "Q118", features(0)),
]
YEARS = {"W26": 1995, "W27": 2025}

LEADER_PAGE = """{| class="wikitable"
|-
! No.
! Title
! Artist(s)
|-
| 1 || "[[Leader Song]]" || [[Gina Lead and the Backups|Gina Lead & the Backups]]
|}"""


@pytest.fixture()
def env(tmp_path):
    conn = db.connect(tmp_path / "p.sqlite")
    for i, (wid, title, credit, item, feats) in enumerate(WORKS):
        conn.execute("INSERT INTO work(work_id, title, canonical_artist, wikidata_qid, selected, canon_rank,"
                     " effective_year) VALUES (?,?,?,?,1,?,?)", (wid, title, credit, item, i + 1, YEARS.get(wid)))
        cur = conn.execute("INSERT INTO candidate(work_id, source, md5, features_json, valid) VALUES (?,?,?,?,1)",
                           (wid, "lakh", f"md5{wid}", feats))
        conn.execute("INSERT INTO selection(work_id, candidate_id) VALUES (?,?)", (wid, cur.lastrowid))
    conn.execute("INSERT INTO candidate(work_id, source, md5, features_json, valid) VALUES ('W13','web','x',?,1)",
                 (features(3),))  # a few stray lyric events in another candidate do not count
    # canon's caches: W01's song item names its performer; a Billboard row links W12's act
    canon = tmp_path / "canon"
    kv = KVCache(canon / "api_cache.sqlite")
    kv.put("wd", "Q100", {"id": "Q100", "label": "Song A", "P31": [SONG], "P175": ["Q200"]})
    kv.put("wd_label", "Q200", "Anna Artist")
    kv.put("wd", "Q114", {"id": "Q114", "label": "Duet Two", "P31": [SONG], "P175": ["Q203"]})  # only Bob
    kv.put("wd_label", "Q203", "Bob Tester")
    kv.put("wd", "Q116", {"id": "Q116", "P31": [SONG], "P175": ["Q213", "Q214"]})
    kv.put("wd", "Q117", {"id": "Q117", "P31": [SONG], "P175": ["Q407", "Q215"]})
    kv.put_many("wd_label", {"Q213": "Nelly", "Q214": "Kelly Tester", "Q407": "The Rolling Stoners",
                             "Q215": "Merry Guest"})
    kv.close()
    (canon / "raw").mkdir(parents=True)
    name = f"wp_{slug(billboard.page_title(2001), 120)}.json"
    (canon / "raw" / name).write_text(json.dumps({"parse": {"wikitext": LEADER_PAGE}}), encoding="utf-8")
    row = billboard.parse_page(LEADER_PAGE, 2001)[0]
    assert row.artist_links == ("Gina Lead and the Backups",)
    conn.execute("INSERT INTO list_entry(list_id, rank, raw_title, raw_artist, work_id) VALUES (?,?,?,?,?)",
                 (row.list_id, row.rank, row.raw_title, row.raw_artist, "W12"))
    conn.commit()
    return conn, tmp_path


def run(env, http, **kw):
    conn, tmp = env
    client = singer.WikiClient(tmp / "themes", http=http, log=lambda s: None, sleep=lambda s: None)
    stats: dict = {}
    out = singer.resolve_singers(conn, [w[0] for w in WORKS], client=client, canon_cache=tmp / "canon",
                                 log=lambda s: None, stats=stats, **kw)
    return out, stats, client


# ---------------------------------------------------------------------------- tests
def test_genders_and_sources(env):
    http = FakeWikimedia()
    out, stats, client = run(env, http)
    g = {k: v["gender"] for k, v in out.items()}
    assert g["W01"] == "female" and out["W01"]["source"] == "p175:person" and out["W01"]["artist_qid"] == "Q200"
    assert g["W02"] == "female" and out["W02"]["source"] == "wikipedia:group-singers"   # decoy film skipped
    assert out["W02"]["artist_qid"] == "Q400"
    assert g["W03"] == "mixed"
    assert g["W04"] == "male" and out["W04"]["source"].endswith("group-members")          # nobody sings
    assert g["W05"] == "female" and out["W05"]["source"].endswith("group-class")
    assert g["W06"] == "male"                                                               # guest dropped
    assert g["W07"] == "mixed" and "duet" in out["W07"]["source"] and out["W07"]["artist_qid"] == "Q203;Q204"
    assert g["W08"] == "female"                                                             # guitarist dropped
    assert g["W09"] == "nonbinary"                                                          # preferred rank
    assert g["W10"] == "female"                                                             # trans woman
    assert g["W11"] == "male" and out["W11"]["source"] == "search:person"                   # film skipped
    assert g["W12"] == "female" and out["W12"]["source"] == "links:leader"
    assert g["W17"] == "unknown" and out["W17"]["source"] == "unresolved"
    assert g["W18"] == "mixed" and out["W18"]["artist_qid"] == "Q211;Q212"                  # "Frank" + surname
    assert g["W19"] == "female" and out["W19"]["source"] == "wikipedia:group-singers"       # P463 members
    assert g["W20"] == "female" and out["W20"]["source"] == "wikipedia:group-lead"          # lead vocalist role
    assert g["W21"] == "female" and out["W21"]["artist_label"] == "Mona Mul"                # "mul" label
    assert g["W22"] == "mixed" and "duet" in out["W22"]["source"]      # P175 "Bob Tester" is not the duet
    # "Nelly Tester" (surname borrowed) must not fuzzy-match "Kelly Tester": falls back to "Nelly"
    assert g["W24"] == "mixed" and out["W24"]["artist_qid"] == "Q213;Q214"
    assert out["W25"]["artist_qid"] == "Q407" and g["W25"] == "male"  # the only performer containing the name
    assert g["W26"] == "female" and g["W27"] == "male"                 # members active in the recording year
    assert set(g.values()) <= set(singer.GENDERS)
    assert stats["network"] > 0 and client.requests["wbsearchentities"] >= 1


def test_instrumental_needs_midi_and_wikidata(env):
    out, _, _ = run(env, FakeWikimedia())
    assert out["W13"]["gender"] == "instrumental" and out["W13"]["source"] == "wikipedia:instrumental-song-type"
    assert out["W16"]["gender"] == "instrumental" and out["W16"]["source"] == "wikipedia:instrumental-song-description"
    assert out["W14"]["gender"] == "male"   # no lyrics in the MIDI, but Wikidata does not say instrumental
    assert out["W15"]["gender"] == "male"   # Wikidata says instrumental, but the MIDI has lyrics
    assert out["W13"]["artist_qid"] == "Q210"
    assert out["W23"]["gender"] == "instrumental" and out["W23"]["source"] == "wikipedia:instrumental-act-non-singing"
    assert out["W28"]["gender"] == "male"   # the work is described as a song: no act-level guess


def test_table_written_and_rerun_is_offline(env):
    conn, _ = env
    first, _, _ = run(env, FakeWikimedia())
    rows = {r["work_id"]: dict(r) for r in conn.execute("SELECT * FROM singer")}
    assert set(rows) == {w[0] for w in WORKS}
    cols = [r[1] for r in conn.execute("PRAGMA table_info(singer)")]
    assert cols == ["work_id", "gender", "source", "artist_qid", "artist_label"]
    assert rows["W02"]["artist_label"] == "The Testers"
    again, stats, client = run(env, Offline())
    assert again == first
    assert stats["network"] == 0 and stats["cache_only"] == len(WORKS) and not client.requests


def test_refresh_refetches(env):
    run(env, FakeWikimedia())
    http = FakeWikimedia()
    conn, tmp = env
    client = singer.WikiClient(tmp / "themes", http=http, refresh=True, log=lambda s: None, sleep=lambda s: None)
    out = singer.resolve_singers(conn, ["W02"], client=client, canon_cache=tmp / "canon", log=lambda s: None)
    assert out["W02"]["gender"] == "female" and http.calls


def test_etiquette_batches_and_maxlag(env):
    conn, tmp = env
    ents = world()
    for i in range(120):  # a big group: its members are fetched in batches of <= 50
        ents[f"Q{5000 + i}"] = entity(f"Q{5000 + i}", f"Member {i}", P31=[HUMAN], P21=[MALE], P106=[MUSICIAN])
    ents["Q400"] = entity("Q400", "The Testers", P31=[ROCK_BAND], P527=[f"Q{5000 + i}" for i in range(120)])
    http = FakeWikimedia(entities=ents, maxlag_first=True)
    slept = []
    client = singer.WikiClient(tmp / "themes", http=http, log=lambda s: None, sleep=slept.append)
    out = singer.resolve_singers(conn, ["W02"], client=client, canon_cache=tmp / "canon", log=lambda s: None)
    assert out["W02"]["gender"] == "male"
    assert slept and slept[0] >= 5  # maxlag: waited, then retried
    wd = [c for c in http.calls if "wikidata" in c["url"]]
    assert wd and all(c["maxlag"] == "5" for c in wd)
    assert all(len(c["ids"].split("|")) <= 50 for c in wd if c["action"] == "wbgetentities")
    wp = [c for c in http.calls if "wikipedia" in c["url"]]
    assert all(len(c["titles"].split("|")) <= 50 for c in wp)


def test_act_names():
    n = singer.act_names("Gladys Knight & the Pips")
    assert (n.lead, n.leader, n.parts) == ("Gladys Knight & the Pips", "Gladys Knight", [])
    n = singer.act_names("Peter, Paul and Mary")
    assert (n.whole, n.lead) == ("Peter, Paul and Mary", "Peter")
    assert singer.act_names("Jo Stafford with Paul Weston").lead == "Jo Stafford"
    assert singer.act_names("Mark Ronson feat. Bruno Mars").whole == "Mark Ronson"
    assert singer.act_names("Elton John & Kiki Dee").parts == [["Elton John"], ["Kiki Dee"]]
    assert singer.act_names("Frank & Nancy Sinatra").parts == [["Frank Sinatra", "Frank"], ["Nancy Sinatra"]]
    assert singer.act_names("Dionne Warwick & Friends").parts == [["Dionne Warwick"]]
    assert singer.act_names("Glenn Miller & his Orchestra").leader == "Glenn Miller"


def test_midi_vocal_evidence():
    fv = singer._features_vocal
    assert fv([]) is None
    assert fv([(json.loads(features(0)), True)]) is False
    assert fv([(json.loads(features(1)), True)]) is True          # any lyric event in the chosen file
    assert fv([(json.loads(features(3)), False)]) is False        # a few stray events elsewhere
    assert fv([(json.loads(features(8)), False)]) is True
    assert fv([(json.loads(features(0, vocal_track=True)), False)]) is True
    assert fv([(json.loads(features(0, karaoke=True)), False)]) is True


def test_person_gender_rules():
    pg = singer.Resolver.person_gender
    assert pg({"P21": [["Q6581097", "normal"]]}) == "male"
    assert pg({"P21": [["Q2449503", "normal"]]}) == "male"            # trans man
    assert pg({"P21": [["Q48270", "normal"]]}) == "nonbinary"
    assert pg({"P21": [["Q6581072", "normal"], ["Q6581097", "normal"]]}) == "unknown"
    assert pg({}) == "unknown"
    assert singer.Resolver.combine(["male", "unknown", "male"]) == "male"
    assert singer.Resolver.combine(["male", "female"]) == "mixed"
