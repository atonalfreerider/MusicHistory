"""``duets.json`` contract: an entry exported from a synthetic duet plan validates, and every
kind of violation is reported."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from mashup_helpers import AXIS, make_track
from musichistory.mashup import duet_contract, duet_export, duet_render
from musichistory.mashup.contract import FRAME
from musichistory.mashup.duet import plan_duet


@pytest.fixture(scope="module")
def doc():
    keys = [(0, "major"), (2, "major"), (7, "major")]
    tracks = [make_track(AXIS * 3, bpm=[120, 110, 126][j], key=keys[j], transpose=keys[j][0], work_id=f"Q{j + 1}")
              for j in range(3)]
    plan = plan_duet(tracks, pair_seconds=16, handoff_bars=2, half_bar=False)
    tl = duet_render.build(plan)
    hop = 256 / 22050
    pitch = []
    for t in plan.tracks:                                 # a voiced tone in each song's own key, 1 s on / 0.25 s off
        n = int(t.duration / hop) + 1
        tt = np.arange(n) * hop
        pitch.append(([None if (x % 1.25) > 1.0 else 60.0 + t.tonic for x in tt], hop))
    meta = [{"work_id": t.work_id, "title": f"Song {t.work_id}", "artist": "Artist", "year": 1960 + j}
            for j, t in enumerate(plan.tracks)]
    entry = duet_export.path_entry("test-path", "Test path", tl, meta, pitch)
    return {"version": 1, "generated_at": "2026-09-30T12:00:00Z", "frame": FRAME, "paths": [entry]}


def test_exported_entry_is_valid(doc):
    assert duet_contract.validate(doc) == []
    p = doc["paths"][0]
    assert p["loops"] is True and p["root"] == "Q1" and p["key"] == "C major"
    assert p["file"] == "test-path/loop.mp3"
    assert [s["kind"] for s in p["segments"]] == ["duet", "handoff"] * 3
    assert p["segments"][0]["start"] == 0 and p["segments"][-1]["end"] == p["seconds"]
    assert len(p["beats"]) == 3 * 8 * 4 and p["beats"][0] == [0.0, 0.0]
    # every song sings; the melody is in the normalized frame, as heard: each song's tonic lands
    # on C (D -2 semitones: 60; G +5: 72)
    for s, want in zip(p["songs"], (60.0, 60.0, 72.0)):
        vals = [v for _, v in s["melody"] if v is not None]
        assert vals and np.allclose(vals, want)
        assert s["vocal_audible"]
    # the root's vocal wraps: it sounds at the very start and the very end of the loop
    va = p["songs"][0]["vocal_audible"]
    assert va[0][0] == 0.0 and va[-1][1] == p["seconds"]
    # chords: what the bed plays (C G Am F), in mix beats
    assert [c[4] for c in p["chords"][:4]] == ["I", "V", "vi", "IV"]
    assert p["chords"][-1][1] == pytest.approx(len(p["beats"]))


def test_files_must_exist_with_root(doc, tmp_path):
    errs = duet_contract.validate(doc, tmp_path)
    assert any("missing" in e for e in errs)
    (tmp_path / "test-path").mkdir()
    (tmp_path / "test-path" / "loop.mp3").write_bytes(b"x")
    assert duet_contract.validate(doc, tmp_path) == []


def _broken(doc, fn):
    d = copy.deepcopy(doc)
    fn(d["paths"][0])
    return duet_contract.validate(d)


@pytest.mark.parametrize("name, fn", [
    ("extra key", lambda p: p.update(lyrics="x")),
    ("missing key", lambda p: p.pop("chords")),
    ("not looping", lambda p: p.update(loops=False)),
    ("gap", lambda p: p["segments"][1].update(start=p["segments"][1]["start"] + 0.5)),
    ("short", lambda p: p["segments"][-1].update(end=p["segments"][-1]["end"] - 1.0)),
    ("one vocal", lambda p: p["segments"][0].update(vocals=["Q1", "Q1"])),
    ("duet with entering", lambda p: p["segments"][0].update(entering="Q2")),
    ("duet over a borrowed bed", lambda p: p["segments"][0].update(instrumental="Q2")),
    ("wrong borrowed", lambda p: p["segments"][1].update(instrumental="Q2")),
    ("leaving still sings", lambda p: p["segments"][1].update(leaving=p["segments"][1]["vocals"][0])),
    ("pairs out of cycle", lambda p: p["segments"][2].update(vocals=["Q3", "Q1"])),
    ("chord match range", lambda p: p["segments"][0].update(chord_match=1.5)),
    ("beats", lambda p: p["beats"].__setitem__(3, [p["beats"][3][0], 7.0])),
    ("chord order", lambda p: p["chords"].reverse()),
    ("roman", lambda p: p["chords"][0].__setitem__(4, "Cmaj")),
    ("root shifted", lambda p: p["songs"][0].update(shift_semitones=2)),
    ("melody range", lambda p: p["songs"][1]["melody"].append([1e6, 60.0])),
    ("melody dense", lambda p: p["songs"][1]["melody"].insert(1, [p["songs"][1]["melody"][0][0] + 0.01, 61.0])),
    ("root first", lambda p: p.update(root="Q2")),
    ("key", lambda p: p.update(key="H major")),
    ("audible", lambda p: p["songs"][2].update(vocal_audible=[[5.0, 2.0]])),
])
def test_violations_reported(doc, name, fn):
    assert _broken(doc, fn), name


def test_doc_level_checks(doc):
    d = copy.deepcopy(doc)
    d["version"] = 2
    d["frame"] = "other"
    d["generated_at"] = "yesterday"
    errs = duet_contract.validate(d)
    assert len(errs) == 3
