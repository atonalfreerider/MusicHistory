"""The cover scheduler (weighted interval scheduling over target notes, repeated songs costed)
and the target loop choice."""

from __future__ import annotations

import numpy as np

from mosaic_helpers import ROOT  # noqa: F401  (puts the repository on sys.path)
from musichistory.mashup.tracks import Track
from musichistory.mosaic import assemble
from musichistory.mosaic.match import Piece
from musichistory.mosaic.notes import SongNotes


def piece(work_id: str, a: int, b: int, weight: float, matched: int | None = None) -> Piece:
    m = matched if matched is not None else b - a + 1
    p = Piece(work_id, a, b, 0, b - a, 1.0, 0.0, 0, m, b - a + 1, b - a + 1, 1.0)
    p.weight = weight
    return p


def test_schedule_finds_the_heaviest_disjoint_set():
    ps = [piece("A", 0, 5, 6.0), piece("B", 0, 2, 4.0), piece("C", 3, 5, 4.0), piece("D", 4, 9, 5.0),
          piece("E", 6, 9, 3.5)]
    chosen, value = assemble.schedule(ps, 10)
    assert [p.work_id for p in chosen] == ["B", "C", "E"]
    assert abs(value - 11.5) < 1e-9
    spans = [(p.a, p.b) for p in chosen]
    assert all(b1 < a2 for (_, b1), (a2, _) in zip(spans, spans[1:]))


def test_schedule_skips_negative_and_out_of_range_pieces():
    chosen, value = assemble.schedule([piece("A", 0, 3, -1.0), piece("B", 2, 12, 9.0), piece("C", 4, 6, 2.0)], 10)
    assert [p.work_id for p in chosen] == ["C"] and value == 2.0


def test_repeated_song_costs_and_is_replaced_when_worth_it():
    ps = [piece("A", 0, 3, 5.0), piece("A", 4, 7, 5.0), piece("B", 4, 7, 4.0)]
    chosen = assemble.cover(ps, 8, dup_cost=2.0)
    assert sorted((p.work_id, p.a) for p in chosen) == [("A", 0), ("B", 4)]
    # when the other song is much weaker the repeat stays
    ps = [piece("A", 0, 3, 5.0), piece("A", 4, 7, 5.0), piece("B", 4, 7, 1.0)]
    chosen = assemble.cover(ps, 8, dup_cost=2.0)
    assert sorted((p.work_id, p.a) for p in chosen) == [("A", 0), ("A", 4)]


def test_prune_keeps_the_best_songs_per_span():
    ps = [piece(w, 0, 4, wt) for w, wt in (("A", 3.0), ("A", 4.0), ("B", 2.0), ("C", 5.0), ("D", 1.0))]
    kept = assemble.prune(ps, keep=2)
    assert sorted((p.work_id, p.weight) for p in kept) == [("A", 4.0), ("C", 5.0)]


def test_mosaic_numbers():
    mo = assemble.Mosaic([piece("A", 0, 4, 1.0, matched=4), piece("B", 6, 9, 1.0, matched=4)], 12)
    assert abs(mo.match - 8 / 12) < 1e-9 and abs(mo.coverage - 9 / 12) < 1e-9
    assert mo.songs == 2 and mo.mean_notes == 4.0


def _song(n_bars: int, sung_bars: set[int], bpm: float = 120.0, bpb: int = 4):
    ibi = 60.0 / bpm
    beats = 0.5 + np.arange(n_bars * bpb + 1) * ibi
    chroma = np.zeros((12, len(beats)))
    for k in range(len(beats)):                                       # C | G | Am | F, repeating
        root = [0, 7, 9, 5][(k // bpb) % 4]
        chroma[[root, (root + 4 - (1 if root == 9 else 0)) % 12, (root + 7) % 12], k] = 1.0
    vocal = np.array([1.0 if (k // bpb) in sung_bars else 0.0 for k in range(len(beats))])
    tr = Track("T", 0, "major", beats, bpb, 0, chroma, vocal, float(beats[-1] + 1))
    rows = []
    for bar in sorted(sung_bars):
        for q in range(4):                                            # four quarter notes per sung bar
            b0 = bar * bpb + q
            rows.append([beats[b0], beats[b0] + 0.4, b0, b0 + 0.8, 60 + q, 1.0, 60.0 + q])
    sn = SongNotes("T", np.array(rows, dtype=float), beats, 0.0, bpb, 0, -5.0, 0.6, float(beats[-1]))
    return tr, sn


def test_choose_loop_prefers_the_densely_sung_phrase():
    tr, sn = _song(16, sung_bars=set(range(4, 12)))
    loop = assemble.choose_loop(tr, sn)
    assert loop is not None and loop.bars == 8 and loop.start_bar == 4
    assert len(loop.notes) == 32 and loop.notes.on.min() == 0.0
    assert loop.seam == 1.0                                           # bar 12 has bar 4's chord


def test_choose_loop_none_without_singing():
    tr, sn = _song(16, sung_bars={2})
    assert assemble.choose_loop(tr, sn) is None
