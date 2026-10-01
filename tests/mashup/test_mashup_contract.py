"""mashups.json: the exporter's output on a synthetic chain validates, and the validator
rejects malformed documents (extra keys such as free text, gaps, dense melodies, ...)."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from mashup_helpers import AXIS, make_track
from musichistory.mashup import contract, export, timeline
from musichistory.mashup.chain import plan_chain


@pytest.fixture(scope="module")
def doc():
    a = make_track(AXIS * 4, bpm=120, work_id="A")
    b = make_track(AXIS * 4, bpm=110, work_id="B", key=(2, "major"), transpose=2)
    plan = plan_chain([a, b], co_seconds=12, full_bars=6, final_bars=4, phrase_bars=8, half_bar=False)
    tl = timeline.build(plan)
    hop = 256 / 22050
    n = int(40 / hop)
    t = np.arange(n) * hop
    # a voiced line that steps through the scale, with gaps
    pitch = [None if (k // 40) % 3 == 2 else round(60 + 2 * np.sin(tt), 2) for k, tt in enumerate(t)]
    meta = [{"work_id": f"Q{j + 1}", "title": f"Song {j + 1}", "artist": "Artist", "year": 1990 + j} for j in range(2)]
    for j, tr in enumerate(plan.tracks):
        tr.work_id = meta[j]["work_id"]
    checks = {h.out_start: {"beat_error_ms": 4.2} for h in plan.hops}
    entry = export.path_entry("demo-path", "Demo", tl, meta, [(pitch, hop), (pitch, hop)], checks, tl.seconds)
    return {"version": contract.VERSION, "generated_at": "2026-09-30T12:00:00Z", "frame": contract.FRAME,
            "paths": [entry]}, tl


def test_export_validates(doc):
    d, tl = doc
    assert contract.validate(d) == []
    p = d["paths"][0]
    assert p["file"] == "demo-path/mix.mp3"
    assert [s["kind"] for s in p["segments"]] == ["full", "changeover", "morph", "full"]
    co = p["segments"][1]
    assert co["vocal"] == "Q2" and co["instrumental"] == "Q1" and co["chord_match"] == pytest.approx(1.0)
    assert co["beat_error_ms"] == 4.2 and co["vocal_shift_semitones"] == -2
    assert p["phrase_beats"] == 32.0 and p["beats_per_bar"] == 4
    beats = np.array(p["beats"])
    assert beats[0].tolist() == [0.0, 0.0] and np.all(np.diff(beats[:, 0]) > 0)
    assert set(np.round(beats[:, 1] % 4, 6)) <= {0.0, 1.0, 2.0, 3.0}


def test_melodies_share_the_frame_and_chords_line_up(doc):
    d, _ = doc
    s1, s2 = d["paths"][0]["songs"]
    v1 = [v for _, v in s1["melody"] if v is not None]
    v2 = [v for _, v in s2["melody"] if v is not None]
    assert v1 and v2
    # song 2 is the same line two semitones up in the source; the frame offsets cancel it
    assert np.median(v2) == pytest.approx(np.median(v1) - 2, abs=0.6)
    # both songs' phrase bar 0 carries the same normalized chord
    assert s1["chords"][0][2] == s2["chords"][0][2] == 0 and s1["chords"][0][4] == "I"
    ivs = s2["vocal_audible"]
    assert ivs[0][0] == pytest.approx(d["paths"][0]["segments"][1]["start"], abs=1e-3)


def _mut(d, fn):
    x = copy.deepcopy(d)
    fn(x["paths"][0])
    return contract.validate(x)


def test_validator_rejects(doc):
    d, _ = doc
    assert _mut(d, lambda p: p["songs"][0].__setitem__("lyrics", "la la"))           # no free text fields
    assert _mut(d, lambda p: p["segments"][1].__setitem__("start", p["segments"][1]["start"] + 0.5))
    assert _mut(d, lambda p: p["segments"][1].__setitem__("vocal", p["segments"][1]["instrumental"]))
    assert _mut(d, lambda p: p["segments"][1].__setitem__("chord_match", None))
    assert _mut(d, lambda p: p["segments"][0].__setitem__("kind", "solo"))
    assert _mut(d, lambda p: p["segments"][0].__setitem__("key", "H major"))
    assert _mut(d, lambda p: p["beats"].append([p["beats"][-1][0] - 1, 0.0]))
    assert _mut(d, lambda p: p["beats"][3].__setitem__(1, p["phrase_beats"]))
    assert _mut(d, lambda p: p["songs"][0]["melody"].extend([[1.0, 60.0], [1.01, 61.0]]))
    assert _mut(d, lambda p: p["songs"][0]["chords"].append([0.0, 4.0, 13, "maj", "I"]))
    assert _mut(d, lambda p: p["songs"][0]["chords"].append([0.0, 4.0, 0, "maj", "hello"]))
    assert _mut(d, lambda p: p["songs"][1].__setitem__("step", 0))
    assert _mut(d, lambda p: p.__setitem__("file", "elsewhere.mp3"))
    assert _mut(d, lambda p: p.__setitem__("phrase_beats", 30.0))
    x = copy.deepcopy(d)
    x["version"] = 2
    assert contract.validate(x)


def test_validator_checks_files(doc, tmp_path):
    d, _ = doc
    assert any("missing" in e for e in contract.validate(d, tmp_path))
    (tmp_path / "demo-path").mkdir()
    (tmp_path / "demo-path" / "mix.mp3").write_bytes(b"\0")
    assert contract.validate(d, tmp_path) == []


def test_despike_drops_tracker_slips():
    pts = [[k / 16, 60.0 + (k % 3) * 0.2] for k in range(20)]
    pts[5][1] = 72.5                     # lone octave error
    pts[9][1] = 30.0                     # far outlier
    pts.insert(12, [12 / 16 + 0.01, None])
    out = export.despike(pts)
    vals = [v for _, v in out if v is not None]
    assert 72.5 not in vals and 30.0 not in vals
    assert out[-1][1] is not None and all(not (a[1] is None and b[1] is None) for a, b in zip(out, out[1:]))
