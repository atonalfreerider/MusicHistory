"""Drum/melody roles from track names vs GM pitched-patch names (review 'fetch-midi' #1, #2).

GM names such as "Percussive Organ", "Steel Drums", "Chrom. Perc" and "Melodic Toms" name
pitched patches: they must not move a track to channel 10 or make it the melody. A track
whose name says drums moves to channel 10 only when its notes look like a drum part.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import midigen  # noqa: E402
from musichistory.midi import process, sanitize  # noqa: E402

DRUMS = {"name": "Drums", "channel": 9, "program": 0, "pitches": [36, 38], "step": 1.0}


def _tracks(p) -> dict[int, dict]:
    return {t["index"]: t for t in p.features["tracks"]}


@pytest.mark.parametrize("name,role", [
    ("Percussive Organ", None), ("PercOrgan", None), ("Perc. Organ", None), ("Percusive Organ", None),
    ("Steel Drums", None), ("STEELDRUM", None), ("StlDrum", None), ("Chrom. Perc", None),
    ("Chromatic Percussion", None), ("Melodic Toms", None), ("Mel. Tom", None), ("Reverse Cymbal", None),
    ("Steel Drum Melody", "melody"), ("ElecPiano/PercOrgan", "piano"),
    # real drum and melody names are unchanged
    ("Drums", "drums"), ("Percussion", "drums"), ("Drum Kit", "drums"), ("Snare", "drums"),
    ("Melody", "melody"), ("Lead Vocal", "vocal"),
])
def test_name_role_skips_gm_pitched_patch_names(name, role):
    assert sanitize.name_role([name.encode("latin-1")]) == role


def test_gm_pitched_patches_stay_on_their_channel():
    tracks = [
        {"name": "Percussive Organ", "channel": 0, "program": 17, "pitches": [60, 64, 67], "step": 2.0, "chord": True},
        {"name": "Steel Drums", "channel": 1, "program": 114, "pitches": [72, 74, 76, 77, 79], "step": 0.5},
        {"name": "Chrom. Perc", "channel": 2, "program": 11, "pitches": [60, 62, 64, 67], "step": 1.0},
        {"name": "PercOrgan", "channel": 3, "program": 17, "pitches": [55, 59, 62], "step": 1.0},
        {"name": "Bass", "channel": 4, "program": 33, "pitches": [36, 43], "step": 1.0},
        DRUMS,
    ]
    p = process(midigen.song(bars=40, tracks=tracks))
    assert p.ok, p.reason
    assert "drums_to_ch10" not in p.features["warnings"]
    t = _tracks(p)
    for i, ch in [(1, 1), (2, 2), (3, 3), (4, 4)]:
        assert t[i]["channel"] == ch and not t[i]["is_drum"], t[i]
        assert (t[i]["role"], t[i]["role_src"]) == ("other", "program"), t[i]
    assert t[1]["family"] == "organ" and t[2]["family"] == "percussive" and t[3]["family"] == "chromatic_percussion"
    assert t[6]["channel"] == 10 and t[6]["role"] == "drums"  # the real kit is untouched
    assert p.features["n_drum_notes"] == t[6]["n_notes"]


def test_melodic_toms_are_not_the_melody():
    tracks = [
        {"name": "Trombone (Melody)", "channel": 0, "program": 57, "pitches": [60, 62, 64, 65, 67], "step": 1.0},
        {"name": "Melodic Toms", "channel": 5, "program": 117, "pitches": [41, 43, 45, 47, 48], "step": 0.25},
        DRUMS,
    ]
    p = process(midigen.song(bars=40, tracks=tracks))
    assert p.ok, p.reason
    t = _tracks(p)
    assert (t[1]["role"], t[1]["role_src"]) == ("melody", "name")
    toms = t[2]
    assert toms["n_notes"] > t[1]["n_notes"]  # busier than the tune: a name 'melody' would win the lead
    assert (toms["role"], toms["role_src"]) == ("other", "program")
    assert toms["channel"] == 6 and not toms["is_drum"]


def test_pitched_part_under_a_drum_name_keeps_its_channel():
    tracks = [
        # a 20-key line named "Drums" (a mislabelled keyboard part)
        {"name": "Drums", "channel": 3, "program": 0, "pitches": list(range(60, 80)), "step": 0.5},
        # a woodblock part above the drum-key range
        {"name": "Perc 2", "channel": 4, "program": 115, "pitches": [90, 92], "step": 1.0},
        {"name": "Bass", "channel": 1, "program": 33, "pitches": [36, 43], "step": 2.0},
    ]
    p = process(midigen.song(bars=40, tracks=tracks))
    assert p.ok, p.reason
    w = p.features["warnings"]
    assert "drums_to_ch10" not in w and "drum_name_pitched_kept" in w
    t = _tracks(p)
    assert (t[1]["channel"], t[1]["is_drum"], t[1]["role"], t[1]["role_src"]) == (4, False, "piano", "program")
    assert (t[2]["channel"], t[2]["is_drum"], t[2]["role"], t[2]["role_src"]) == (5, False, "other", "program")
    assert not p.features["has_drums"]


@pytest.mark.parametrize("n_keys,moved", [(3, True), (12, True), (13, False)])
def test_drum_like_rule_boundary(n_keys, moved):
    keys = [35, 36, 38, 40, 42, 44, 46, 49, 51, 57, 41, 43, 45][:n_keys]
    tracks = [
        {"name": "Melody", "channel": 0, "program": 73, "pitches": [72, 74, 76], "step": 1.0},
        {"name": "Drum Kit", "channel": 15, "program": 0, "pitches": keys, "step": 0.5},
    ]
    p = process(midigen.song(bars=40, tracks=tracks))
    kit = _tracks(p)[2]
    assert (kit["channel"] == 10) is moved and kit["is_drum"] is moved
    assert kit["role"] == ("drums" if moved else "piano")
