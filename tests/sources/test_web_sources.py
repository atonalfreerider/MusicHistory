"""Web adapters with a fake HTTP client: parsing, the freemidi session flow, midicollection."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "midi"))

import midigen  # noqa: E402
from musichistory import config, db  # noqa: E402
from musichistory.sources import base, freemidi, midicollection, midiworld  # noqa: E402
from musichistory.sources.base import Work  # noqa: E402

FREEMIDI_SEARCH = """<h2>Songs</h2><div class=container><div class=row>
<div class="col-md-4 mb-4"><div class=card><div class=card-body>
<h5 class=card-title><a href=download3-26270-despacito-artists-bands title=Despacito>Despacito</a></h5>
<div class=card-text>
<a href="/artists">artists</a>
</div></div></div></div>
<div class="col-md-4 mb-4"><div class=card><div class=card-body>
<h5 class=card-title><a href=download3-26714-luis-fonsi-despacito-artists-bands title="luis fonsi - despacito">luis fonsi - despacito</a></h5>
<div class=card-text>
<a href="/artists">artists</a>
</div></div></div></div>
<div class="col-md-4 mb-4"><div class=card><div class=card-body>
<h5 class=card-title><a href=download3-12322-city-of-blinding-lights-u2 title="City of Blinding Lights">City of Blinding Lights</a></h5>
<div class=card-text>
<a href="/artist-1234-u2">U2</a>
</div></div></div></div>
</div></div>"""

MIDIWORLD_SEARCH = """<h3>Search result:</h3><br><ul>
<li>
Bohemian Rhapsody (Queen) - <a href="https://www.midiworld.com/download/3476" target="_blank">download</a>
</li>
<li>
Bohemian Rhapsody (Walt Disney) - <a href="https://www.midiworld.com/download/6746" target="_blank">download</a>
</li></ul>"""


@dataclass
class Resp:
    status: int
    content: bytes = b""
    headers: dict = field(default_factory=dict)
    from_cache: bool = False
    content_type: str = "text/html"

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class FakeClient:
    def __init__(self, routes: dict[str, Resp]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, *, params=None, headers=None, use_cache=True, allow_redirects=True, **_):
        if params:
            url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self.calls.append((url, {"headers": headers, "use_cache": use_cache, "allow_redirects": allow_redirects}))
        return self.routes.get(url, Resp(404))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "CANDIDATES", tmp_path / "data" / "candidates")
    monkeypatch.setattr(config, "CACHE", tmp_path / "cache")
    c = db.connect(tmp_path / "data" / "p.sqlite")
    base.ensure_schema(c)
    return c


DESPACITO = Work("Q30080545", "Despacito", "Luis Fonsi featuring Daddy Yankee", ["Luis Fonsi featuring Daddy Yankee"])


def test_freemidi_parse_and_pick():
    cards = freemidi.parse_search(FREEMIDI_SEARCH)
    assert [c["id"] for c in cards] == ["26270", "26714", "12322"]
    assert cards[2]["artist"] == "U2" and cards[2]["artist_href"] == "/artist-1234-u2"
    hit, rejected = freemidi.best_hit(cards, DESPACITO)
    assert hit is not None and hit.source_ref == "26714"  # artist inside the title of a generic upload
    assert rejected == 1                                   # the bare "Despacito" upload has no artist


def test_freemidi_query():
    assert freemidi.query_for("Hey Ya!") == "Hey Ya"
    assert freemidi.query_for("(I Can't Get No) Satisfaction") == "Satisfaction"
    assert freemidi.query_for("Uptown Funk (feat. Bruno Mars)") == "Uptown Funk"


def test_freemidi_two_step_session_download(conn):
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, search_artists, work_year, canon_rank, in_pool)"
                 " VALUES ('Q30080545','Despacito','Luis Fonsi featuring Daddy Yankee','[]',2017,1,1)")
    page = "https://freemidi.org/download3-26714-luis-fonsi-despacito-artists-bands"
    client = FakeClient({
        "https://freemidi.org/search?q=Despacito": Resp(200, FREEMIDI_SEARCH.encode()),
        page: Resp(200, b"<html>song page</html>"),
        "https://freemidi.org/getter-26714": Resp(200, midigen.song(), {"Content-Disposition": 'attachment; filename="Despacito.mid"'}),
    })
    st = freemidi.fetch(conn, base.load_works(conn), client)
    assert st.valid == 1 and st.requests == 3
    urls = [u for u, _ in client.calls]
    assert urls == ["https://freemidi.org/search?q=Despacito", page, "https://freemidi.org/getter-26714"]
    getter_opts = client.calls[2][1]
    assert getter_opts["headers"]["Referer"] == page and getter_opts["allow_redirects"] is False
    assert client.calls[1][1]["use_cache"] is False and getter_opts["use_cache"] is False
    row = conn.execute("SELECT source, source_ref, orig_name, valid FROM candidate").fetchone()
    assert tuple(row) == ("freemidi", "26714", "Despacito.mid | luis fonsi - despacito (artists)", 1)
    # works with >= 2 valid candidates are not searched
    conn.execute("DELETE FROM fetch_status")
    client.calls.clear()
    freemidi.fetch(conn, base.load_works(conn), client, below=1)
    assert client.calls == []


def test_freemidi_redirect_without_session_is_a_failure(conn):
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, search_artists, work_year, canon_rank, in_pool)"
                 " VALUES ('Q30080545','Despacito','Luis Fonsi featuring Daddy Yankee','[]',2017,1,1)")
    page = "https://freemidi.org/download3-26714-luis-fonsi-despacito-artists-bands"
    client = FakeClient({
        "https://freemidi.org/search?q=Despacito": Resp(200, FREEMIDI_SEARCH.encode()),
        page: Resp(200, b"<html></html>"),
        "https://freemidi.org/getter-26714": Resp(302, b""),
    })
    st = freemidi.fetch(conn, base.load_works(conn), client)
    assert st.failed == 1 and st.valid == 0
    assert conn.execute("SELECT status, detail FROM fetch_attempt").fetchone()[:] == ("download_failed", "getter HTTP 302")


def test_midiworld_parse():
    items = midiworld.parse_search(MIDIWORLD_SEARCH)
    assert [(i["id"], i["title"], i["artist"]) for i in items] == [
        ("3476", "Bohemian Rhapsody", "Queen"), ("6746", "Bohemian Rhapsody", "Walt Disney")]
    hits, rejected = midiworld.hits_for(items, Work("Q", "Bohemian Rhapsody", "Queen", ["Queen"]))
    assert [h.source_ref for h in hits] == ["3476"] and rejected == 1


SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://midicollection.com/sitemap-artists.xml</loc></sitemap>
<sitemap><loc>https://midicollection.com/sitemap-songs-1.xml</loc></sitemap></sitemapindex>"""
ARTISTS = b"""<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://midicollection.com/artist/queen</loc></url></urlset>"""
SONGS = b"""<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://midicollection.com/song/62941/queen-bohemian-rhapsody</loc></url>
<url><loc>https://midicollection.com/song/100/bohemian-rhapsody-queen2-bb80</loc></url>
<url><loc>https://midicollection.com/song/101/bohemian-rhapsody1</loc></url>
<url><loc>https://midicollection.com/song/102/queensryche-bohemian-rhapsody</loc></url></urlset>"""
ARTIST_PAGE = b"""<ul class="songs"><li>
<button class="midi-play-btn" data-midi-id="62941" data-url="/midi/MIDI/br.mid" data-title="Bohemian Rhapsody">&#9654;</button>
</li></ul>"""
SONG_PAGE = b"""<script type="application/ld+json">{"@context":"https://schema.org","@type":"MusicRecording","name":"Bohemian Rhapsody","byArtist":{"@type":"MusicGroup","name":"Queen"}}</script>
<button class="midi-play-btn" data-midi-id="100" data-url="/midi/MIDI/bohemian-rhapsody-queen2%20bb80.mid" data-title="x">&#9654; Play</button>"""


def test_midicollection_offline_index_and_download(conn, tmp_path):
    mc_dir = config.CACHE / "midicollection"
    mc_dir.mkdir(parents=True)
    (mc_dir / "sitemap.xml").write_bytes(SITEMAP)
    (mc_dir / "sitemap-artists.xml").write_bytes(ARTISTS)
    (mc_dir / "sitemap-songs-1.xml").write_bytes(SONGS)
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, search_artists, work_year, canon_rank, in_pool)"
                 " VALUES ('Q187745','Bohemian Rhapsody','Queen','[\"Queen\"]',1975,6,1)")
    song_a, song_b = midigen.song(bars=30), midigen.song(bars=31)
    client = FakeClient({
        "https://midicollection.com/artist/queen": Resp(200, ARTIST_PAGE),
        "https://midicollection.com/song/100/bohemian-rhapsody-queen2-bb80": Resp(200, SONG_PAGE),
        "https://midicollection.com/midi/MIDI/br.mid": Resp(200, song_a),
        "https://midicollection.com/midi/MIDI/bohemian-rhapsody-queen2%20bb80.mid": Resp(200, song_b),
    })
    mc = midicollection.MidiCollection(None)
    hits, rejected = mc.search(Work("Q187745", "Bohemian Rhapsody", "Queen", ["Queen"]))
    assert sorted(h.source_ref for h in hits) == ["100", "62941"]
    assert rejected == 1  # title only (101); 102 names another act and is no title match at all
    st = midicollection.fetch(conn, base.load_works(conn), client)
    assert st.valid == 2
    urls = [u for u, _ in client.calls]
    assert "https://midicollection.com/artist/queen" in urls
    assert not any("?q=" in u for u in urls)  # the site search is disallowed by robots.txt
    names = {r[0] for r in conn.execute("SELECT orig_name FROM candidate")}
    assert names == {"br.mid", "bohemian-rhapsody-queen2 bb80.mid"}
    assert all(not opts["use_cache"] for u, opts in client.calls if "/midi/MIDI/" in u)


def test_midicollection_fetches_missing_sitemaps(conn):
    client = FakeClient({
        "https://midicollection.com/sitemap.xml": Resp(200, SITEMAP),
        "https://midicollection.com/sitemap-artists.xml": Resp(200, ARTISTS),
        "https://midicollection.com/sitemap-songs-1.xml": Resp(200, SONGS),
    })
    mc = midicollection.MidiCollection(client)
    assert [u for u, _ in client.calls] == ["https://midicollection.com/sitemap.xml",
                                            "https://midicollection.com/sitemap-artists.xml",
                                            "https://midicollection.com/sitemap-songs-1.xml"]
    assert (config.CACHE / "midicollection" / "sitemap-songs-1.xml").read_bytes() == SONGS
    assert mc.artists == ["queen"]
    # a second instance works offline from the cached files and index
    client.calls.clear()
    midicollection.MidiCollection(None)
    assert client.calls == []


# ---------------------------------------------------------------- --redownload-web
# Raw web downloads are never kept, so a sanitizer change reaches web candidates only by
# downloading them again; the rows are updated in place (candidate_id stays stable).

def _freemidi_stored(conn):
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, search_artists, work_year, canon_rank, in_pool)"
                 " VALUES ('Q30080545','Despacito','Luis Fonsi featuring Daddy Yankee','[]',2017,1,1)")
    page = "https://freemidi.org/download3-26714-luis-fonsi-despacito-artists-bands"
    routes = {
        "https://freemidi.org/search?q=Despacito": Resp(200, FREEMIDI_SEARCH.encode()),
        page: Resp(200, b"<html>song page</html>"),
        "https://freemidi.org/getter-26714": Resp(200, midigen.song()),
    }
    assert freemidi.fetch(conn, base.load_works(conn), FakeClient(routes)).valid == 1
    conn.execute("UPDATE candidate SET sha256='stale', features_json=NULL")
    conn.commit()
    return page, routes


def test_freemidi_redownload_updates_in_place(conn):
    page, routes = _freemidi_stored(conn)
    before = tuple(conn.execute("SELECT candidate_id, md5, orig_name, url, sanitized_path FROM candidate").fetchone())
    client = FakeClient(routes)
    st = freemidi.redownload(conn, base.load_works(conn), client)
    assert (st.hits, st.valid, st.failed, st.changed) == (1, 1, 0, 0)
    # song page from the (cached) search, then the session page and the getter with Referer
    assert [u for u, _ in client.calls] == ["https://freemidi.org/search?q=Despacito", page,
                                            "https://freemidi.org/getter-26714"]
    assert client.calls[1][1]["use_cache"] is False
    assert client.calls[2][1] == {"headers": {"Referer": page}, "use_cache": False, "allow_redirects": False}
    row = conn.execute("SELECT candidate_id, md5, orig_name, url, sanitized_path, sha256, features_json, valid"
                       " FROM candidate").fetchone()
    assert tuple(row)[:5] == before and row["valid"] == 1
    assert row["sha256"] != "stale" and row["features_json"]
    assert conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 1


def test_freemidi_redownload_takes_the_song_page_from_the_request_log(conn):
    page, routes = _freemidi_stored(conn)
    conn.execute("INSERT INTO fetch_log(url, status) VALUES (?, 200)", (page,))
    client = FakeClient(routes)
    assert freemidi.redownload(conn, base.load_works(conn), client).valid == 1
    assert [u for u, _ in client.calls] == [page, "https://freemidi.org/getter-26714"]


def _midicollection_stored(conn):
    mc_dir = config.CACHE / "midicollection"
    mc_dir.mkdir(parents=True)
    (mc_dir / "sitemap.xml").write_bytes(SITEMAP)
    (mc_dir / "sitemap-artists.xml").write_bytes(ARTISTS)
    (mc_dir / "sitemap-songs-1.xml").write_bytes(SONGS)
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, search_artists, work_year, canon_rank, in_pool)"
                 " VALUES ('Q187745','Bohemian Rhapsody','Queen','[\"Queen\"]',1975,6,1)")
    routes = {
        "https://midicollection.com/artist/queen": Resp(200, ARTIST_PAGE),
        "https://midicollection.com/song/100/bohemian-rhapsody-queen2-bb80": Resp(200, SONG_PAGE),
        "https://midicollection.com/midi/MIDI/br.mid": Resp(200, midigen.song(bars=30)),
        "https://midicollection.com/midi/MIDI/bohemian-rhapsody-queen2%20bb80.mid": Resp(200, midigen.song(bars=31)),
    }
    assert midicollection.fetch(conn, base.load_works(conn), FakeClient(routes)).valid == 2
    conn.execute("UPDATE candidate SET sha256='stale'")
    conn.commit()
    return routes


def test_midicollection_redownload_by_stored_url(conn):
    routes = _midicollection_stored(conn)
    before = {r[0]: tuple(r) for r in conn.execute("SELECT url, candidate_id, md5, sanitized_path FROM candidate")}
    changed = midigen.song(bars=33)  # the site now serves other bytes for one file
    routes["https://midicollection.com/midi/MIDI/br.mid"] = Resp(200, changed)
    client = FakeClient(routes)
    st = midicollection.redownload(conn, base.load_works(conn), client)
    assert (st.valid, st.changed, st.failed) == (2, 1, 0)
    assert sorted(u for u, _ in client.calls) == sorted(before)  # only the stored file URLs
    assert all(opts["use_cache"] is False for _, opts in client.calls)
    after = {r[0]: tuple(r) for r in conn.execute("SELECT url, candidate_id, md5, sanitized_path FROM candidate")}
    br = "https://midicollection.com/midi/MIDI/br.mid"
    other = next(u for u in before if u != br)
    assert after[other] == before[other]
    assert after[br][1] == before[br][1]  # same candidate_id ...
    assert after[br][2] == hashlib.md5(changed).hexdigest() != before[br][2]  # ... new md5 and file
    assert base.resolve(after[br][3]).exists() and not base.resolve(before[br][3]).exists()
    assert conn.execute("SELECT COUNT(*) FROM candidate WHERE sha256='stale'").fetchone()[0] == 0


def test_redownload_failure_leaves_the_row(conn):
    _midicollection_stored(conn)
    st = midicollection.redownload(conn, base.load_works(conn), FakeClient({}))  # every URL 404s now
    assert (st.failed, st.valid) == (2, 0) and st.failures == {"HTTP 404": 2}
    assert conn.execute("SELECT COUNT(*) FROM candidate WHERE sha256='stale' AND valid=1").fetchone()[0] == 2


def test_fetch_stage_redownload_web_option(conn, monkeypatch):
    import argparse

    from musichistory.sources import stage

    conn.execute("INSERT INTO work(work_id, title, canonical_artist, search_artists, work_year, canon_rank, in_pool)"
                 " VALUES ('Q187745','Bohemian Rhapsody','Queen','[\"Queen\"]',1975,6,1)")
    items = midiworld.parse_search(MIDIWORLD_SEARCH)
    hit = midiworld.hits_for(items, base.load_works(conn)[0])[0][0]
    assert base.ingest(conn, base.load_works(conn)[0], hit, midigen.song(bars=30)) == "new"
    conn.execute("UPDATE candidate SET sha256='stale'")
    conn.commit()
    client = FakeClient({"https://www.midiworld.com/download/3476": Resp(200, midigen.song(bars=30))})
    monkeypatch.setattr(stage, "client", lambda c: client)
    monkeypatch.setattr(stage.db, "connect", lambda *a, **k: conn)
    p = argparse.ArgumentParser()
    stage.add_arguments(p)
    assert stage.run(p.parse_args(["--sources", "midiworld", "--redownload-web", "--no-hooktheory"])) == 0
    assert [u for u, _ in client.calls] == ["https://www.midiworld.com/download/3476"]  # no search
    assert conn.execute("SELECT sha256 FROM candidate").fetchone()[0] != "stale"
