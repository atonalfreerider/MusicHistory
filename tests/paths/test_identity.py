"""Identity audibility checks on per-beat chord/bass readings."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.paths import identity as ident  # noqa: E402


def lab(root: int, minor: bool = False) -> int:
    return (root % 12) * 2 + (1 if minor else 0)


def beats(*chords, n: int = 4) -> list[int]:
    out = []
    for c in chords:
        out += [c] * n
    return out


def test_parse_roman_and_degrees():
    assert ident.parse_roman("vi-IV-I-V") == [(9, "min"), (5, "maj"), (0, "maj"), (7, "maj")]
    assert ident.parse_roman("vi-V-IV-III") == [(9, "min"), (7, "maj"), (5, "maj"), (4, "maj")]
    assert ident.parse_roman("viio-III-vi") == [(11, "dim"), (4, "maj"), (9, "min")]
    assert ident.parse_roman("I-bVII-IV") == [(0, "maj"), (10, "maj"), (5, "maj")]
    assert ident.parse_degrees("b7-6-b6-5") == [10, 9, 8, 7]
    assert ident.parse_degrees("5-#4-4-3") == [7, 6, 5, 4]
    with pytest.raises(ValueError):
        ident.parse_roman("I-x")


def test_frame_shift_relative_normalization():
    assert ident.frame_shift(0, "major") == 0
    assert ident.frame_shift(9, "minor") == 0      # A minor is the frame
    assert ident.frame_shift(4, "minor") == 7      # E minor: frame A -> E
    assert ident.frame_shift(7, "major") == 7


def test_axis_progression_in_g():
    chords = beats(lab(7), lab(2), lab(4, True), lab(0)) * 2        # G D Em C
    c = ident.check_clip("schema", "axis progression I-V-vi-IV", "I-V-vi-IV", chords, [], 7, "major")
    assert c.checked and c.in_key and c.any_key
    wrong = ident.check_clip("schema", "axis progression I-V-vi-IV", "I-V-vi-IV", chords, [], 2, "major")
    assert not wrong.in_key and wrong.any_key       # the pattern is there, the key reading differs


def test_rotation_and_relative_minor():
    # Andalusian cadence in E minor: Em D C B (frame vi-V-IV-III), starting mid-cycle.
    chords = beats(lab(0), lab(11), lab(4, True), lab(2), lab(0), lab(11))
    c = ident.check_clip("schema", "Andalusian cadence i-bVII-bVI-V", "vi-V-IV-III", chords, [], 4, "minor")
    assert c.in_key


def test_two_chord_vamp_needs_two_cycles():
    once = beats(lab(0), lab(5), lab(7))
    twice = beats(lab(0), lab(5), lab(0), lab(5))
    assert not ident.check_clip("schema", "two-chord vamp I-IV", "I-IV", once, [], 0, "major").in_key
    assert ident.check_clip("schema", "two-chord vamp I-IV", "I-IV", twice, [], 0, "major").in_key


def test_cadence_in_order():
    chords = beats(lab(2, True), lab(7), lab(0))                      # Dm G C
    assert ident.check_clip("progression", "ii-V-I cadence", "ii-V-I", chords, [], 0, "major").in_key
    rev = beats(lab(0), lab(7), lab(2, True))
    assert not ident.check_clip("progression", "ii-V-I cadence", "ii-V-I", rev, [], 0, "major").in_key


def test_quality_tolerance_only_for_long_cycles():
    three = beats(lab(0), lab(7), lab(5, True), lab(0))              # I-V-iv, looking for I-V-IV
    assert not ident.find_cycle(three, ident.parse_roman("I-V-IV"), 0)
    four = beats(lab(0), lab(7), lab(9), lab(5))                      # VI major instead of vi
    assert ident.find_cycle(four, ident.parse_roman("I-V-vi-IV"), 0)


def test_one_beat_noise_is_tolerated():
    chords = beats(lab(0), lab(7)) + [lab(3)] + beats(lab(9, True), lab(5))
    assert ident.find_cycle(chords, ident.parse_roman("I-V-vi-IV"), 0)


def test_twelve_bar_blues():
    roman = "I-I-I-I-IV-IV-I-I-V-IV-I-I"
    bars = [0, 0, 0, 0, 5, 5, 0, 0, 7, 5, 0, 0]
    chords = [lab(r + 9) for r in bars for _ in range(4)]            # in A
    assert ident.check_clip("progression", "12-bar blues", roman, [lab(9)] * 2 + chords, [], 9, "major").in_key
    assert not ident.check_clip("progression", "12-bar blues", roman, beats(lab(9), lab(2)) * 6, [], 9,
                                "major").in_key
    quick = [0, 5, 0, 0, 5, 5, 0, 0, 7, 7, 0, 7]                       # quick change + V turnaround
    chords = [lab(r) for r in quick for _ in range(4)]
    assert ident.find_blues(chords, 0)


def test_descending_chromatic_bass():
    bass = [0, 0, 11, 11, 10, 10, 9, 9, 5]                           # C B Bb A: 1-7-b7-6
    c = ident.check_clip("progression", "descending chromatic bass 1-7-b7-6", "1-7-b7-6", [], bass, 0, "major")
    assert c.in_key
    c2 = ident.check_clip("progression", "descending chromatic bass 1-7-b7-6", "1-7-b7-6", [], bass, 2, "major")
    assert not c2.in_key and c2.any_key


def test_strong_is_a_pair_check():
    c = ident.check_clip("strong", "exact melody passage, 19 notes", None, [], [], 0, "major")
    assert not c.checked and c.note == "pair check"


def test_common_chord_run_in_frame():
    a = ident.relative_chords(beats(lab(7), lab(2), lab(4, True), lab(0)), 7, "major")   # G major
    b = ident.relative_chords(beats(lab(0), lab(7), lab(9, True), lab(5), lab(2)), 0, "major")
    assert a == [lab(0), lab(7), lab(9, True), lab(5)]
    assert ident.longest_common_run(a, b) == 4


def test_window_similarity():
    rng = np.random.default_rng(3)
    A = np.abs(rng.normal(size=(12, 20)))
    assert ident.best_window_similarity(A, A) == pytest.approx(1.0)
    B = np.concatenate([np.abs(rng.normal(size=(12, 5))), A[:, 4:14], np.abs(rng.normal(size=(12, 5)))], axis=1)
    assert ident.best_window_similarity(A, B) == pytest.approx(1.0)
    assert ident.best_window_similarity(A[:, :3], B) == 0.0
