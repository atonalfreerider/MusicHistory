"""``mosaics.json`` validation: a well-formed document passes; wrong keys, overlapping or
self-sourced pieces, sections out of order or not whole loops, bad ranges and names fail."""

from __future__ import annotations

import copy

import pytest

from mosaic_helpers import ROOT  # noqa: F401
from musichistory.mosaic import contract, export
from musichistory.mosaic.contract import FRAME, validate

LOOP_S = 16.0
LB = 32


def valid_doc() -> dict:
    n_loops = 5
    beats = [[round(k * LOOP_S + q * 0.5, 3), float(q)] for k in range(n_loops) for q in range(LB)]
    song = {"title": "Song", "artist": "Artist", "year": 1970}
    return {"version": 1, "generated_at": "2026-10-01T00:00:00Z", "frame": FRAME, "mosaics": [{
        "id": "song-mosaic", "name": "Song, Reassembled", "title": "Song rebuilt from 2 songs",
        "target": {"work_id": "Q1", **song}, "file": "song-mosaic/mix.mp3", "seconds": n_loops * LOOP_S,
        "key": "C major", "bpm": 120.0, "beats_per_bar": 4, "loop_beats": LB,
        "sections": [{"start": 0.0, "end": 16.0, "kind": "original", "loops": 1},
                     {"start": 16.0, "end": 64.0, "kind": "mosaic", "loops": 3},
                     {"start": 64.0, "end": 80.0, "kind": "harmony", "loops": 1}],
        "beats": beats, "chords": [[0.0, 4.0, 0, "maj", "I"], [4.0, 8.0, 7, "maj", "V"], [8.0, 12.0, 9, "min", "vi"]],
        "notes": [[0.0, 0.9, 60], [1.0, 1.9, 62], [2.0, 3.9, 64], [8.0, 9.0, 67]],
        "pieces": [{"work_id": "Q2", **song, "start": 0.0, "end": 3.9, "source_start": 10.2, "source_end": 12.5,
                    "shift_semitones": -3, "tempo_ratio": 0.92, "match": 1.0,
                    "notes": [[0.02, 0.9, 60], [1.01, 1.88, 62], [2.0, 3.8, 64]]},
                   {"work_id": "Q3", **song, "start": 8.0, "end": 9.0, "source_start": 3.0, "source_end": 4.1,
                    "shift_semitones": 5, "tempo_ratio": 1.21, "match": 0.75, "notes": [[8.05, 9.0, 55]]}],
        "harmonies": [{"work_id": "Q4", **song, "shift_semitones": 2, "tempo_ratio": 1.0, "consonance": 0.85,
                       "notes": [[0.0, 1.0, 57], [1.0, 2.0, 59]]}],
        "coverage": 1.0, "match": 0.9}]}


def test_valid_document_passes():
    assert validate(valid_doc()) == []


def test_file_must_exist_with_root(tmp_path):
    assert any("missing" in e for e in validate(valid_doc(), tmp_path))
    (tmp_path / "song-mosaic").mkdir()
    (tmp_path / "song-mosaic" / "mix.mp3").write_bytes(b"x")
    assert validate(valid_doc(), tmp_path) == []


def mutate(fn) -> list[str]:
    doc = copy.deepcopy(valid_doc())
    fn(doc["mosaics"][0])
    return validate(doc)


@pytest.mark.parametrize("fn, needle", [
    (lambda m: m.update(extra=1), "keys differ"),
    (lambda m: m["pieces"][0].pop("match"), "keys differ"),
    (lambda m: m["harmonies"][0].update(lyrics="la"), "keys differ"),
    (lambda m: m["pieces"][1].update(start=3.0), "overlaps"),
    (lambda m: m["pieces"][0].update(work_id="Q1"), "another song"),
    (lambda m: m["pieces"][0].update(shift_semitones=7), "shift_semitones"),
    (lambda m: m["pieces"][0].update(tempo_ratio=1.9), "tempo_ratio"),
    (lambda m: m["pieces"].clear(), "no pieces"),
    (lambda m: m["harmonies"].clear(), "one or two harmonies"),
    (lambda m: m["harmonies"].append(copy.deepcopy(m["harmonies"][0])), "another song"),
    (lambda m: m["sections"].reverse(), "original, mosaic, harmony"),
    (lambda m: m["sections"][1].update(loops=2), "whole loops"),
    (lambda m: m.update(match=1.0, coverage=0.8), "exceeds coverage"),
    (lambda m: m.update(name="A Name Far Too Long To Fit On The Screen"), "name"),
    (lambda m: m.update(seconds=95.0), "seconds"),
    (lambda m: m["beats"].pop(), "beats"),
    (lambda m: m["notes"].append([33.0, 34.0, 60]), "out of range"),
    (lambda m: m["chords"].append([10.0, 9.0, 0, "maj", "I"]), "chords"),
    (lambda m: m.update(loop_beats=30), "whole number of bars"),
    (lambda m: m.update(key="H major"), "key"),
    (lambda m: m.update(file="other/mix.mp3"), "file must be"),
])
def test_violations(fn, needle):
    errs = mutate(fn)
    assert errs and any(needle in e for e in errs), errs


def test_another_loop_must_not_fit():
    def short(m):
        m["seconds"] = 64.0
        m["sections"][2].update(start=48.0, end=64.0)
        m["sections"][1].update(end=48.0, loops=2)
        m["beats"] = m["beats"][:4 * LB]
    assert any("another whole loop" in e for e in mutate(short))


def test_names():
    mid, name, title = export.names("I Don't Want to Miss a Thing", 6)
    assert mid == "i-dont-want-to-miss-a-thing-mosaic" and len(name) <= contract.NAME_MAX
    assert title == "I Don't Want to Miss a Thing rebuilt from 6 songs"
    assert export.names("Yesterday", 5) == ("yesterday-mosaic", "Yesterday, Reassembled", "Yesterday rebuilt from 5 songs")
    assert export.names("(They Long to Be) Close to You", 3)[1] == "Close to You, Reassembled"
    assert export.names("Tonight's the Night (Gonna Be Alright)", 4)[1] == "Tonight's the Night, Reassembled"
