"""paths.json v2 contract: schema and handoff consistency; the real file when it exists."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory import config  # noqa: E402
from musichistory.paths import contract  # noqa: E402
from musichistory.paths.plan import plan_step  # noqa: E402


def make_doc() -> dict:
    songs = [("Q1", "A", "X", 1960, "C major", 0, 100.0), ("Q2", "B", "Y", 1970, "D major", 2, 120.0),
             ("Q3", "C", "Z", 1990, "Bb minor", 10, 60.0)]
    steps, prev = [], None
    for i, (wid, title, artist, year, key, tonic, bpm) in enumerate(songs):
        p = plan_step(None if prev is None else prev[0], None if prev is None else prev[1], tonic, bpm)
        steps.append({"work_id": wid, "title": title, "artist": artist, "year": year,
                      "file": contract.step_file("demo-path", i, wid), "seconds": 29.5, "via": None if i == 0 else
                      {"identity": "axis progression I-V-vi-IV", "strong": False, "z": 0.0, "edge_kind": "tree",
                       "family_size": 40},
                      "start_key": key if prev is None else steps[-1]["key"], "key": key,
                      "start_semitones": p.start_semitones, "start_bpm": round(p.start_bpm, 2), "bpm": bpm,
                      "morph_seconds": round(p.morph_seconds, 3), "key_source": "audio", "bpm_source": "audio"})
        prev = (tonic, bpm)
    return {"version": 2, "generated_at": "2026-09-30T12:00:00Z", "morph_bars": 2,
            "paths": [{"id": "demo-path", "title": "Demo", "subtitle": "1960 -> 1990 · 3 songs · chord progression",
                       "description": "Each song shares the axis progression.", "identity": "axis progression I-V-vi-IV",
                       "seconds": 88.5, "steps": steps}]}


def test_valid_doc_passes():
    doc = make_doc()
    assert contract.validate(doc) == []
    s = doc["paths"][0]["steps"]
    assert s[1]["start_semitones"] == -2 and s[2]["start_semitones"] == 4     # D -> Bb: wrap(2 - 10)
    assert s[1]["start_bpm"] == 100.0 and s[1]["morph_seconds"] == pytest.approx(4.8)


def test_fold_is_enforced():
    doc = make_doc()
    step = doc["paths"][0]["steps"][2]
    assert step["start_bpm"] == pytest.approx(60.0)        # 120 heard, 60 native: folded to 60
    step["start_bpm"] = 120.0
    assert any("folded" in e for e in contract.validate(doc))


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d["paths"][0]["steps"][1].__setitem__("start_key", "E major"), "previous step's key"),
    (lambda d: d["paths"][0]["steps"][1].__setitem__("start_semitones", 3), "Wrap"),
    (lambda d: d["paths"][0]["steps"][0].__setitem__("start_semitones", 1), "natively"),
    (lambda d: d["paths"][0]["steps"][0].__setitem__("via", {"identity": "x"}), "via must be null"),
    (lambda d: d["paths"][0]["steps"][1].__setitem__("file", "demo-path/2_Q2.mp3"), "file must be"),
    (lambda d: d["paths"][0]["steps"][1].pop("bpm_source"), "differ from the contract"),
    (lambda d: d["paths"][0].__setitem__("seconds", 10.0), "sum of the steps"),
    (lambda d: d["paths"][0].__setitem__("id", "Not A Slug"), "slug"),
    (lambda d: d.__setitem__("version", 1), "version"),
    (lambda d: d["paths"][0]["steps"][1]["via"].__setitem__("edge_kind", "loop"), "edge_kind"),
    (lambda d: d["paths"][0]["steps"][1].__setitem__("key_source", "guess"), "audio|midi"),
    (lambda d: d["paths"][0]["steps"][1].__setitem__("key", "H major"), "not a key name"),
    (lambda d: d["paths"][0].__setitem__("steps", d["paths"][0]["steps"][:2]), "3-6 steps"),
])
def test_violations_are_caught(mutate, needle):
    doc = copy.deepcopy(make_doc())
    mutate(doc)
    errors = contract.validate(doc)
    assert any(needle in e for e in errors), errors


def test_key_names():
    assert contract.key_tonic("C major") == 0
    assert contract.key_tonic("F# minor") == 6
    assert contract.key_tonic("Bb major") == 10
    assert contract.key_tonic("Db major") == 1
    with pytest.raises(ValueError):
        contract.key_tonic("C dorian")


def test_missing_files_are_reported(tmp_path):
    doc = make_doc()
    assert any("missing" in e for e in contract.validate(doc, tmp_path))
    for s in doc["paths"][0]["steps"]:
        f = tmp_path / s["file"]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    assert contract.validate(doc, tmp_path) == []


def test_real_paths_json():
    path = config.DATA / "audio" / "renders" / "paths.json"
    if not path.exists():
        pytest.skip("paths.json not rendered yet")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert contract.validate(doc, path.parent) == []
    assert 10 <= len(doc["paths"]) <= 12
    for p in doc["paths"]:
        assert "lyric" not in p["description"].lower()
