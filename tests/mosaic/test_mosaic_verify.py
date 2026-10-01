"""Heard-match note comparison: onset tolerance, octave folding, unrounded pitches, durations."""

from __future__ import annotations

from mosaic_helpers import ROOT  # noqa: F401
from musichistory.mosaic.match import Seq
from musichistory.mosaic.verify import note_match


def test_note_match_folds_octaves_only_when_asked():
    target = Seq("T", [0.0, 1.0, 2.0, 3.0], [0.9, 1.9, 2.9, 3.9], [60, 62, 64, 65], 0.5)
    heard = Seq("H", [0.05, 0.9, 2.3, 3.0], [0.9, 1.8, 2.9, 3.2], [48, 62, 64, 65], 0.5,
                fpitch=[48.2, 62.4, 64.0, 65.0])
    folded = note_match(target, heard, octave=True)
    exact = note_match(target, heard, octave=False)
    assert list(folded) == [True, True, False, False]       # 3rd note late, 4th too short
    assert list(exact) == [False, True, False, False]
    assert list(note_match(target, heard, octave=True, durations=False)) == [True, True, False, True]


def test_note_match_empty_heard():
    target = Seq("T", [0.0], [1.0], [60], 0.5)
    assert not note_match(target, Seq("H", [], [], []), octave=True).any()
