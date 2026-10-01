"""Checks for the narration scripts (musichistory/narration/scripts/<path id>.json, DESIGN §15).

Every script must: follow the schema; anchor its cues to real segments of the path's mashup in
data/audio/mashups/mashups.json, in time order and inside the mix; fit each cue in the time before
the next cue (2.4 words per second, a 1 s gap; the last cue before the mix ends); stay silent for
the first 2 s of every changeover so the new voice is heard; speak only declarative sentences
(no '?', '!', ellipses or quotation marks, final period); keep digits and roman numerals out of
the spoken text; and cite at least one http(s) source per cue.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "musichistory" / "narration" / "scripts"
MASHUPS = ROOT / "data" / "audio" / "mashups" / "mashups.json"

WORDS_PER_SECOND = 2.4
GAP_SECONDS = 1.0           # silence left before the next cue
TAIL_SECONDS = 0.5          # the last cue ends this long before the mix does
CHANGEOVER_QUIET = 2.0      # no narration over the first seconds of a changeover
KINDS = {"intro", "song", "changeover", "outro"}
CUE_KEYS = {"id", "anchor", "kind", "text", "image", "sources"}
CUE_ID_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
IMAGE_RE = re.compile(r"^artist-Q[1-9][0-9]*$")
# Roman-numeral chord symbols (IV, vi, bVII, V7, #iv ...). A lone "I" is allowed: it is the
# pronoun and appears in titles such as "What'd I Say".
ROMAN_RE = re.compile(r"(?<![A-Za-z])[b#♭♯]?(?:VII|VI|IV|V|III|II|vii|vi|iv|v|iii|ii)(?:7|°|ø|maj7)?(?![A-Za-z'’])")
ROMAN_CHAIN_RE = re.compile(r"(?<![A-Za-z])[b#]?[IViv]+\s*[–—-]\s*[b#]?[IViv]+(?![A-Za-z])")


def words(text: str) -> int:
    """Spoken words; hyphenated compounds count once per part ("sixty-three" is two)."""
    return len(re.findall(r"[^\W_]+(?:['’][^\W_]+)?", text))


def _load_mashups() -> dict:
    data = json.loads(MASHUPS.read_text(encoding="utf-8"))
    return {p["id"]: p for p in data["paths"]}


MIXES = _load_mashups() if MASHUPS.exists() else {}
FILES = sorted(SCRIPTS.glob("*.json")) if SCRIPTS.is_dir() else []


def _at(cue: dict, mix: dict) -> float:
    return float(mix["segments"][cue["anchor"]["segment"]]["start"]) + float(cue["anchor"]["offset"])


def test_scripts_exist_for_every_mashup():
    assert MIXES, "mashups.json missing"
    assert {f.stem for f in FILES} == set(MIXES), "one script per mashup path"


@pytest.fixture(params=FILES, ids=[f.stem for f in FILES])
def script(request):
    doc = json.loads(request.param.read_text(encoding="utf-8"))
    return request.param, doc


def test_schema(script):
    path, doc = script
    assert set(doc) == {"id", "title", "cues"}
    assert doc["id"] == path.stem
    assert isinstance(doc["title"], str) and doc["title"].strip()
    cues = doc["cues"]
    assert isinstance(cues, list) and 4 <= len(cues) <= 8, "aim for 4-8 cues"
    assert cues[0]["kind"] == "intro" and cues[-1]["kind"] == "outro"
    ids = [c["id"] for c in cues]
    assert len(ids) == len(set(ids)), "cue ids unique"
    for c in cues:
        assert set(c) == CUE_KEYS, c.get("id")
        assert CUE_ID_RE.match(c["id"]), c["id"]
        assert c["kind"] in KINDS
        a = c["anchor"]
        assert set(a) == {"segment", "offset"}
        assert isinstance(a["segment"], int) and not isinstance(a["segment"], bool)
        assert isinstance(a["offset"], (int, float)) and not isinstance(a["offset"], bool)
        assert isinstance(c["text"], str) and c["text"].strip()
        assert c["image"] is None or (isinstance(c["image"], str) and IMAGE_RE.match(c["image"])), c["image"]
        assert isinstance(c["sources"], list)
        for s in c["sources"]:
            assert set(s) == {"title", "url"}
            assert isinstance(s["title"], str) and s["title"].strip()


def test_title_matches_mashup(script):
    _, doc = script
    assert doc["title"] == MIXES[doc["id"]]["title"]


def test_anchors_valid(script):
    _, doc = script
    mix = MIXES[doc["id"]]
    segs = mix["segments"]
    last = -1.0
    for c in doc["cues"]:
        i, off = c["anchor"]["segment"], c["anchor"]["offset"]
        assert 0 <= i < len(segs), f"{c['id']}: segment {i}"
        seg = segs[i]
        assert 0 <= off < float(seg["end"]) - float(seg["start"]), f"{c['id']}: offset {off} outside segment {i}"
        at = _at(c, mix)
        assert at > last, f"{c['id']}: cues must be in time order"
        assert at < float(mix["seconds"])
        last = at


def test_changeover_cues_sit_in_changeovers(script):
    _, doc = script
    mix = MIXES[doc["id"]]
    for c in doc["cues"]:
        if c["kind"] == "changeover":
            assert mix["segments"][c["anchor"]["segment"]]["kind"] == "changeover", c["id"]


def test_word_rate_budget(script):
    _, doc = script
    mix = MIXES[doc["id"]]
    cues = doc["cues"]
    ats = [_at(c, mix) for c in cues]
    for k, c in enumerate(cues):
        room = (ats[k + 1] - GAP_SECONDS) if k + 1 < len(cues) else (float(mix["seconds"]) - TAIL_SECONDS)
        budget = WORDS_PER_SECOND * (room - ats[k])
        n = words(c["text"])
        assert n <= budget, f"{c['id']}: {n} words, budget {budget:.1f}"


def test_changeover_openings_are_clear(script):
    _, doc = script
    mix = MIXES[doc["id"]]
    starts = [float(s["start"]) for s in mix["segments"] if s["kind"] == "changeover"]
    for c in doc["cues"]:
        at = _at(c, mix)
        end = at + words(c["text"]) / WORDS_PER_SECOND
        for s in starts:
            assert not (s <= at < s + CHANGEOVER_QUIET), f"{c['id']}: starts in the first 2 s of a changeover"
            if at < s:
                assert end <= s + 1e-6, f"{c['id']}: still speaking at the changeover at {s:.2f}s"


def test_delivery(script):
    _, doc = script
    for c in doc["cues"]:
        t = c["text"]
        assert "?" not in t and "!" not in t, c["id"]
        assert "..." not in t and "…" not in t, c["id"]
        assert not re.search(r"[\"“”]", t), f"{c['id']}: no quotations in narration"
        assert t.rstrip().endswith("."), f"{c['id']}: must end with a period"
        assert t == " ".join(t.split()), f"{c['id']}: single spaces"


def test_no_digits_or_roman_numerals(script):
    _, doc = script
    for c in doc["cues"]:
        t = c["text"]
        assert not re.search(r"\d", t), f"{c['id']}: spell numbers out"
        m = ROMAN_RE.search(t) or ROMAN_CHAIN_RE.search(t)
        assert not m, f"{c['id']}: roman numeral {m.group(0)!r} in spoken text"


def test_sources(script):
    _, doc = script
    for c in doc["cues"]:
        assert len(c["sources"]) >= 1, c["id"]
        for s in c["sources"]:
            assert s["url"].startswith("https://") or s["url"].startswith("http://"), s


def test_validator_helpers():
    assert words("sixty-three years, Søren's band.") == 5
    assert ROMAN_RE.search("one, five, six minor, four") is None
    assert ROMAN_RE.search("the I–V–vi–IV loop")
    assert ROMAN_RE.search("a bVII chord")
    assert ROMAN_RE.search("What'd I Say") is None
    assert ROMAN_CHAIN_RE.search("I-vi-ii-V")
