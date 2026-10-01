"""Note segmentation of a pitch track: steady notes, vibrato kept in one note, scoops absorbed,
repeated notes split at onsets, short blips dropped, and the beat conversion."""

from __future__ import annotations

import numpy as np

from mosaic_helpers import ROOT  # noqa: F401
from musichistory.mosaic import notes

HOP = 256 / 22050


def track(*parts: tuple[float, float | None]) -> np.ndarray:
    """Concatenated (seconds, MIDI pitch or None) parts at the analysis hop."""
    out = []
    for secs, p in parts:
        n = int(round(secs / HOP))
        out.append(np.full(n, np.nan if p is None else float(p)))
    return np.concatenate(out)


def test_steady_notes_and_rests():
    p = track((0.3, 60), (0.2, None), (0.4, 62), (0.3, 64))
    got = notes.segment(p, HOP)
    assert [n[2] for n in got] == [60, 62, 64]
    assert abs(got[0][1] - 0.3) < 0.03 and abs(got[1][0] - 0.5) < 0.03 and abs(got[2][0] - 0.9) < 0.03
    assert all(n[3] > 0.9 for n in got)


def test_vibrato_stays_one_note_and_tuning_is_removed():
    t = np.arange(int(0.8 / HOP)) * HOP
    p = 64.3 + 0.4 * np.sin(2 * np.pi * 5.5 * t)
    got = notes.segment(p, HOP, tuning=0.3)
    assert len(got) == 1 and got[0][2] == 64


def test_scoop_is_absorbed_by_the_next_note():
    p = np.r_[np.linspace(56.8, 59.8, int(0.12 / HOP)), track((0.4, 60))]
    got = notes.segment(p, HOP)
    assert len(got) == 1 and got[0][2] == 60 and got[0][0] < 0.02


def test_repeated_note_split_at_an_onset():
    p = track((0.6, 67))
    assert len(notes.segment(p, HOP)) == 1
    got = notes.segment(p, HOP, onsets=[0.3])
    assert [n[2] for n in got] == [67, 67] and abs(got[1][0] - 0.3) < 0.02


def test_short_blips_are_dropped():
    p = track((0.05, 70), (0.2, None), (0.3, 60))
    assert [n[2] for n in notes.segment(p, HOP)] == [60]


def test_beats_round_trip_with_extrapolation():
    beats = np.array([0.5, 1.0, 1.5, 2.0])
    q = notes.to_beats([0.0, 0.75, 2.5], beats)
    assert np.allclose(q, [-1.0, 0.5, 4.0])
    assert np.allclose(notes.to_seconds(q, beats), [0.0, 0.75, 2.5])


def test_regrid_counts_like_the_track():
    from musichistory.mashup.tracks import Track

    beats = 0.2 + np.arange(33) * 0.25                                # 240 BPM tracker grid
    rows = np.array([[0.2 + 0.25 * k, 0.2 + 0.25 * k + 0.2, 0, 0, 60 + k % 5, 1.0, 60 + k % 5] for k in range(0, 32, 2)])
    rows[:, 2] = notes.to_beats(rows[:, 0], beats)
    rows[:, 3] = notes.to_beats(rows[:, 1], beats)
    sn = notes.SongNotes("S", rows, beats, 0.0, 4, 1, -5.0, 0.5, 9.0)
    tr = Track("S", 0, "major", beats, 4, 1, np.zeros((12, len(beats))), np.zeros(len(beats)), 9.0)
    half, tr_half = sn.regrid(0.5), tr.regrid(0.5)
    assert np.array_equal(half.beats, tr_half.beats) and half.downbeat_phase == tr_half.phase
    assert abs(half.bpm - 120.0) < 1e-6 and half.factor == 0.5
    assert np.allclose(notes.to_seconds(half.notes[:, 2], half.beats), rows[:, 0])
    double, tr_double = sn.regrid(2), tr.regrid(2)
    assert np.array_equal(double.beats, tr_double.beats) and double.downbeat_phase == tr_double.phase
    assert np.allclose(double.notes[:, 2], 2 * rows[:, 2])
