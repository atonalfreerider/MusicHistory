"""Bar grid: full bars, the dominant meter and a first_downbeat in phase with it."""

from __future__ import annotations

from id_helpers import slim

from musichistory.identity import extract
from musichistory.identity.meter import Meter


def on_grid(fd: float, bpb: float, beats: list[float]) -> bool:
    return all(abs(((b - fd) / bpb) - round((b - fd) / bpb)) < 1e-6 for b in beats)


def test_first_downbeat_is_a_bar_line_of_the_dominant_meter():
    """Review 'analyze' (first_downbeat phase): consumers use first_downbeat + k * beats_per_bar
    as the bar lines, so an odd first bar, a pickup or an early meter change must not set it."""
    # A 5/4 first bar, then 4/4 (Q7033371-like): real bar lines 5, 9, 13, ...
    m = Meter([(0.0, 5, 4, 1), (5.0, 4, 4, 20)])
    assert m.beats_per_bar() == 4.0
    assert m.first_downbeat(1.0) == 1.0 and on_grid(1.0, 4.0, [5.0, 9.0, 13.0, 81.0])
    assert m.first_downbeat(6.0) == 5.0
    # A half-beat pickup bar (Q466255-like): the grid starts at 0.5, after the first note.
    m = Meter([(0.0, 1, 8, 1), (0.5, 4, 4, 30)])
    assert m.first_downbeat(0.0) == 0.5
    # An early meter change: 4 bars of 4/4, one 2/4 bar, then 40 bars of 4/4 on the new phase.
    m = Meter([(0.0, 4, 4, 4), (16.0, 2, 4, 1), (18.0, 4, 4, 40)])
    assert m.first_downbeat(0.0) == 2.0 and m.first_downbeat(7.0) == 6.0
    # A short first bar kept as its own run by the slim: its bar is not full.
    m = Meter([(0.0, 4, 4, 1), (0.25, 4, 4, 20)])
    assert m.bars()[:2] == [(0.0, 4.0, False), (0.25, 4.0, True)]
    assert m.first_downbeat(0.25) == 0.25 and m.first_downbeat(0.1) == 0.25 and m.first_downbeat(9.0) == 8.25
    # Plain songs keep the bar of the first note.
    m = Meter([(0.0, 4, 4, 50)])
    assert m.first_downbeat(4.5) == 4.0 and m.first_downbeat(0.0) == 0.0
    m = Meter([(0.0, 3, 4, 50)])
    assert m.first_downbeat(7.0) == 6.0


def test_beats_per_bar_counts_full_bars():
    # 10 bars of 4/4 and 10 of 3/4 plus a short 3/4 bar: a tie on full bars goes to the longer bar.
    m = Meter([(0.0, 3, 4, 1), (1.0, 3, 4, 10), (31.0, 4, 4, 10)])
    assert m.beats_per_bar() == 4.0
    mixed = Meter([(0.0, 4, 4, 2), (8.0, 3, 4, 2)])
    assert mixed.beats_per_bar() == 4.0 and mixed.bar_start(12.5) == 11.0


def test_extract_first_downbeat_with_an_odd_first_bar():
    notes = [[1.0, 1.0, 72, 3, 3, 1.0]] + [[5.0 + 4 * k + j, 1.0, 72 + j, 3, 3, 1.0] for k in range(20) for j in range(4)]
    chords = [[5.0 + 4 * k, 9.0 + 4 * k, 0, ""] for k in range(20)]
    s = slim(chords, notes, end_beat=85.0, measures=[[0.0, 5, 4, 1], [5.0, 4, 4, 20]])
    ident = extract.extract(s, None)
    assert ident.beats_per_bar == 4.0 and ident.first_downbeat == 1.0
    assert on_grid(ident.first_downbeat, ident.beats_per_bar, [5.0, 9.0, 45.0])
