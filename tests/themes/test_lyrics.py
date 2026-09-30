"""Transient lyric reader: decoding, candidate planning, reading without writes.

Every lyric string here is an invented placeholder sentence written for these tests (never
real song lyrics).
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import mido
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "midi"))

import midigen  # noqa: E402
from musichistory import db  # noqa: E402
from musichistory.themes import lyrics as L  # noqa: E402

# Invented placeholder lines (not from any song).
PLACEHOLDER = [
    "paper boats drift across the kitchen floor",
    "the kettle hums a tune about the rain",
    "seven pigeons argue on the railing",
    "my bicycle remembers every hill",
    "the lamp is tired but it keeps on glowing",
    "we count the buttons on a yellow coat",
]


def _midi_with_meta(events: list[tuple[str, bytes]], *, kar: bool = False) -> bytes:
    """A small song plus one extra track of ('lyrics'|'text', raw bytes) meta events."""
    mid = mido.MidiFile(file=io.BytesIO(midigen.song(bars=20)))
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("track_name", name="Words", time=0))
    if kar:
        tr.append(mido.MetaMessage("text", text="@KMIDI KARAOKE FILE", time=0))
        tr.append(mido.MetaMessage("text", text="@LENGL", time=0))
    buf = io.BytesIO()
    mid.tracks.append(tr)
    mid.save(file=buf)
    data = bytearray(buf.getvalue())
    # Append the raw meta events by hand so arbitrary bytes (cp1252) survive untouched.
    body = bytearray()
    for kind, payload in events:
        mtype = 0x05 if kind == "lyrics" else 0x01
        body += bytes([60, 0xFF, mtype]) + _vlq(len(payload)) + payload
    body += bytes([0, 0xFF, 0x2F, 0])
    # Replace the last track (which mido wrote with only its header events) by ours.
    last = data.rfind(b"MTrk")
    head_len = int.from_bytes(data[last + 4:last + 8], "big")
    old = data[last + 8:last + 8 + head_len]
    old = old[:-4]  # drop its end_of_track
    new = old + body
    data[last:] = b"MTrk" + len(new).to_bytes(4, "big") + new
    return bytes(data)


def _vlq(n: int) -> bytes:
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def _lyric_events(lines: list[str]) -> list[tuple[str, bytes]]:
    """One event per word with a trailing space; a CR ends each line (common .mid style)."""
    ev = []
    for line in lines:
        words = line.split()
        for i, w in enumerate(words):
            ev.append(("lyrics", (w + ("\r" if i == len(words) - 1 else " ")).encode("ascii")))
    return ev


def _kar_events(lines: list[str]) -> list[tuple[str, bytes]]:
    """Soft Karaoke style: '/' starts a line, syllables split with no hyphen, leading spaces."""
    ev = []
    for line in lines:
        for i, w in enumerate(line.split()):
            prefix = "/" if i == 0 else " "
            if len(w) > 5:  # split long words into two syllables
                ev += [("text", (prefix + w[:3]).encode()), ("text", w[3:].encode())]
            else:
                ev.append(("text", (prefix + w).encode()))
    return ev


# ------------------------------------------------------------------ syllables
def test_spaced_syllables_join_into_words_and_lines():
    syl = ["/pa", "per ", "boats ", "drift", "\\the ", "ket-", "tle ", "hums"]
    assert L.syllables_to_lines(syl) == ["paper boats drift", "the kettle hums"]


def test_unspaced_events_are_words_and_hyphens_join():
    syl = ["seven", "pig-", "eons", "argue", "\r", "on", "the", "rail-", "ing"]
    assert L.syllables_to_lines(syl) == ["seven pigeons argue", "on the railing"]


def test_cr_lf_and_junk_characters():
    syl = ["my ", "bi", "cycle ", "re", "members\n", "ev", "ery ", "[hill]\r\n"]
    assert L.syllables_to_lines(syl) == ["my bicycle remembers", "every hill"]


def test_long_unbroken_text_is_resplit():
    words = " ".join(PLACEHOLDER).split()
    lines = L._resplit([" ".join(words)])
    assert len(lines) > 3 and all(len(x.split()) <= L.MAX_LINE_WORDS for x in lines)


# ------------------------------------------------------------------ decode_midi
def test_decode_lyric_meta_events():
    res = L.decode_midi(_midi_with_meta(_lyric_events(PLACEHOLDER)))
    assert res is not None
    kind, lines = res
    assert kind == "lyric" and lines == PLACEHOLDER


def test_decode_soft_karaoke_text_events():
    res = L.decode_midi(_midi_with_meta(_kar_events(PLACEHOLDER), kar=True))
    assert res is not None
    kind, lines = res
    assert kind == "kar" and lines == PLACEHOLDER


def test_cp1252_fallback_and_credit_lines_dropped():
    lines = PLACEHOLDER + ["the café window fogs"]
    ev = [("lyrics", b"Sequenced by nobody at example.com\r")]
    for line in lines:
        ev += [("lyrics", (w + " ").encode("cp1252")) for w in line.split()] + [("lyrics", b"\r")]
    kind, out = L.decode_midi(_midi_with_meta(ev))
    assert out[-1] == "the café window fogs"
    assert not any("example" in x for x in out)


def test_too_few_words_is_not_lyrics():
    assert L.decode_midi(_midi_with_meta(_lyric_events(["la la la"] * 3))) is None
    assert L.decode_midi(b"not a midi file") is None


def test_duplicate_lyric_track_is_not_doubled():
    data = _midi_with_meta(_lyric_events(PLACEHOLDER))
    raw = L.validate.parse(data)
    lyr = [(e.tick, e.data) for e in raw.tracks[-1] if e.meta == L.META_LYRIC]
    assert L._pick_tracks([lyr, list(lyr)]) == sorted(lyr, key=lambda e: e[0])


def test_lyrics_repr_never_shows_text():
    ly = L.Lyrics(PLACEHOLDER, 7, "lakh", "lyric")
    assert "paper" not in repr(ly) and "paper" not in str(ly) and "6 lines" in repr(ly)


# ------------------------------------------------------------------ plan / read
GOOD = _midi_with_meta(_lyric_events(PLACEHOLDER))
GARBAGE = b"MThd not really a midi file"
KAR = _midi_with_meta(_kar_events(PLACEHOLDER), kar=True)


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _fixture_db(tmp_path: Path):
    conn = db.connect(tmp_path / "p.sqlite")
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, selected) VALUES ('W1','Song One','Act',1)")
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, selected) VALUES ('W2','Song Two','Act',1)")
    conn.execute("INSERT INTO work(work_id, title, canonical_artist, selected) VALUES ('W3','Song Three','Act',0)")
    rows = [
        # id, work, source, md5, chosen, valid, features
        (1, "W1", "lakh", _md5(GOOD), 0, 1, {"lyric_events": 300}),
        (2, "W1", "lakh_clean", _md5(GARBAGE), 1, 1, {"lyric_events": 40}),
        (3, "W1", "lakh", "c" * 32, 0, 1, {"lyric_events": 5}),
        (4, "W2", "freemidi", "d" * 32, 1, 1, {"lyric_events": 0, "text_events": 250, "karaoke": True}),
        (5, "W2", "lakh", _md5(GOOD), 0, 1, {"lyric_events": 60}),
        (6, "W3", "lakh", "f" * 32, 1, 1, {"lyric_events": 500}),
    ]
    for cid, w, s, md5, ch, v, f in rows:
        conn.execute("INSERT INTO candidate(candidate_id, work_id, source, source_ref, md5, chosen, valid, features_json,"
                     " url) VALUES (?,?,?,?,?,?,?,?,?)", (cid, w, s, md5, md5, ch, v, json.dumps(f),
                                                          "https://example.invalid/x" if s == "freemidi" else None))
    conn.commit()
    return conn


def test_plan_orders_chosen_then_events_and_skips_unselected(tmp_path):
    conn = _fixture_db(tmp_path)
    p = L.plan(conn)
    assert set(p) == {"W1", "W2"}
    assert [c.candidate_id for c in p["W1"]] == [2, 1]
    assert [(c.candidate_id, c.kind) for c in p["W2"]] == [(4, "kar"), (5, "lyric")]


def test_read_streams_lakh_once_and_falls_back(tmp_path, monkeypatch):
    conn = _fixture_db(tmp_path)
    files = {_md5(GOOD): GOOD, _md5(GARBAGE): GARBAGE}
    calls = []

    def fake_lakh(md5s):
        calls.append(set(md5s))
        for m in md5s:
            yield m, files[m]

    monkeypatch.setattr(L, "_lakh_bytes", fake_lakh)
    monkeypatch.setattr(L, "_web_bytes", lambda *a, **k: pytest.fail("web must not be used with web=False"))
    out, st = L.read(conn, L.plan(conn), web=False, progress=lambda s: None)
    assert len(calls) == 1 and calls[0] == set(files)          # one pass for every Lakh file
    # W1: the chosen candidate 2 does not decode -> falls back to candidate 1.
    assert out["W1"].candidate_id == 1 and out["W1"].lines == PLACEHOLDER
    # W2: its first choice is a web file, skipped -> Lakh candidate 5 (same bytes as W1's).
    assert out["W2"].candidate_id == 5
    assert st.web_skipped == 1 and st.web_needed == 1 and st.fallback_used == 2 and st.decode_failed == 1


def test_read_web_candidate_in_memory(tmp_path, monkeypatch):
    conn = _fixture_db(tmp_path)
    monkeypatch.setattr(L, "_lakh_bytes", lambda md5s: iter(()))
    monkeypatch.setattr(L, "_web_client", lambda: object())
    monkeypatch.setattr(L, "_web_bytes", lambda conn, client, c: KAR if c.source == "freemidi" else None)
    before = set(tmp_path.rglob("*"))
    out, st = L.read(conn, L.plan(conn, ["W2"]), web=True, progress=lambda s: None)
    assert out["W2"].candidate_id == 4 and out["W2"].kind == "kar" and out["W2"].lines == PLACEHOLDER
    assert st.web_downloaded == 1 and st.web_changed == 1        # stored md5 was a placeholder
    # Nothing new on disk except the SQLite files of the fixture DB.
    new = {p for p in set(tmp_path.rglob("*")) - before if not p.name.startswith("p.sqlite")}
    assert not new
