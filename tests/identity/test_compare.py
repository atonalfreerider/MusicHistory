"""Alignment scores: the vectorized Gotoh against a plain reference, agreements, Hooktheory."""

from __future__ import annotations

import random
from types import SimpleNamespace

import numpy as np
import pytest

from musichistory.identity import compare
from musichistory.identity.chords import l1


def reference_sw(sub, go, ge):
    n, m = sub.shape
    neg = -1e18
    h = [[0.0] * (m + 1) for _ in range(n + 1)]
    e = [[neg] * (m + 1) for _ in range(n + 1)]
    f = [[neg] * (m + 1) for _ in range(n + 1)]
    best = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            e[i][j] = max(e[i - 1][j] - ge, h[i - 1][j] - go)
            f[i][j] = max(f[i][j - 1] - ge, h[i][j - 1] - go)
            h[i][j] = max(0.0, h[i - 1][j - 1] + sub[i - 1, j - 1], e[i][j], f[i][j])
            best = max(best, h[i][j])
    return best


def test_vectorized_gotoh_matches_the_reference():
    rng = random.Random(3)
    for _ in range(60):
        n, m = rng.randint(1, 25), rng.randint(1, 25)
        sub = np.array([[rng.choice([2.0, 0.5, -0.4, -1.0, 1.5]) for _ in range(m)] for _ in range(n)])
        go, ge = rng.choice([(2.5, 0.75), (3.0, 0.3), (1.0, 1.0)])
        assert compare.smith_waterman(sub, go, ge) == pytest.approx(reference_sw(sub, go, ge))


def ident(chg=None, pitches=None, met=None, fifth=False):
    def tokens(kind="chg", level="L1"):
        return list(chg or [])
    mel = None if pitches is None else SimpleNamespace(pitches=pitches, met=met or [0] * len(pitches))
    return SimpleNamespace(tokens=tokens, melody=mel, key_ambiguous_fifth=fifth, normalization="relative")


AXIS = [l1(0, ""), l1(7, ""), l1(9, "m"), l1(5, "")]


def test_chord_agreement():
    a = ident(AXIS * 8)
    assert compare.chord_agreement(a, a) == 1.0
    half = ident(AXIS * 4)
    assert compare.chord_agreement(a, half) == pytest.approx(1 / np.sqrt(2), abs=1e-3)
    shifted = ident([((t // 3 + 2) % 12) * 3 + t % 3 for t in AXIS * 8])
    assert compare.chord_agreement(a, shifted) < 0.3
    assert compare.chord_agreement(a, ident(AXIS[:3])) is None
    # A fifth-ambiguous key lets the other song match a fifth away (minus a small penalty).
    fifth = ident([((t // 3 + 7) % 12) * 3 + t % 3 for t in AXIS * 8], fifth=True)
    assert compare.chord_agreement(a, fifth) > 0.9


def test_melody_agreement_merges_repeated_notes():
    tune = [60, 62, 64, 65, 67, 65, 64, 62] * 6
    a = ident(pitches=tune)
    assert compare.melody_agreement(a, a) == 1.0
    split = []
    for p in tune:
        split += [p, p]  # every syllable split in two
    assert compare.melody_agreement(a, ident(pitches=split)) == 1.0
    other = ident(pitches=[61, 66, 70, 59, 63, 68, 58, 71] * 6)
    assert compare.melody_agreement(a, other) < 0.2
    assert compare.melody_agreement(a, ident(pitches=None)) is None


def test_hooktheory_section_normalized_into_our_frame():
    # A major section: A E F#m D (I V vi IV) with a tune on the tonic triad.
    ann = {"num_beats": 16, "meters": [{"beat": 0, "beats_per_bar": 4, "beat_unit": 4}],
           "keys": [{"beat": 0, "tonic_pitch_class": 9, "scale_degree_intervals": [2, 2, 1, 2, 2, 2]}],
           "harmony": [{"onset": 4 * i, "offset": 4 * i + 4, "root_pitch_class": r, "root_position_intervals": iv,
                        "inversion": 0} for i, (r, iv) in enumerate([(9, [4, 3]), (4, [4, 3]), (6, [3, 4]), (2, [4, 3])])],
           "melody": [{"onset": i, "offset": i + 1, "octave": 0, "pitch_class": pc}
                      for i, pc in enumerate([9, 11, 1, 2, 4, 2, 1, 11, 9, 1, 4, 9, 4, 1, 11, 9])]}
    frame = compare.hooktheory_frame(ann)
    assert frame["chg"] == AXIS
    assert list(frame["pcs"][:4]) == [0, 2, 4, 5]
    song_mel = [(p + 3) + 60 for p in (9, 11, 1, 2, 4, 2, 1, 11, 9, 1, 4, 9, 4, 1, 11, 9)]
    song = ident(AXIS * 6, pitches=[p % 12 + 60 for p in song_mel] * 3, met=[0, 1, 1, 1] * 12)
    assert compare.hooktheory_agreement(song, [{"annotations": ann}]) > 0.9
    unrelated = ident([l1(2, "m"), l1(4, ""), l1(11, "dim"), l1(1, "")] * 6, pitches=[61, 66, 70, 59] * 12)
    assert compare.hooktheory_agreement(unrelated, [ann]) < 0.4
    assert compare.hooktheory_agreement(song, []) is None
    minor = compare.hooktheory_frame({"keys": [{"beat": 0, "tonic_pitch_class": 4, "scale_degree_intervals":
                                                [2, 1, 2, 2, 1, 2]}],
                                      "harmony": [{"onset": 0, "offset": 4, "root_pitch_class": 4,
                                                   "root_position_intervals": [3, 4]}], "melody": []})
    assert minor["chg"] == [l1(9, "m")]  # E minor -> A minor (relative normalization)
