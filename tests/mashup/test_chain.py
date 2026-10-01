"""Alignment math and the chain planner on synthetic songs."""

from __future__ import annotations

import numpy as np
import pytest

from mashup_helpers import AXIS, label, make_track
from musichistory.mashup import chain
from musichistory.mashup.chain import (OutBar, bar_agreement, bar_sequence, beat_agreement, loop_period, plan_chain,
                                       shift_label, window_agreement)


def test_beat_agreement_levels():
    C, Cm, Am, Em, F, D = (label(x) for x in ("C", "Cm", "Am", "Em", "F", "D"))
    assert beat_agreement(C, C) == 1.0
    assert beat_agreement(C, Cm) == 0.5 and beat_agreement(C, Am) == 0.5 and beat_agreement(C, Em) == 0.5
    assert beat_agreement(C, F) == 0.0 and beat_agreement(C, D) == 0.0
    assert beat_agreement(-1, C) is None


def test_shift_label_and_bar_agreement():
    assert shift_label(label("A"), 3) == label("C")
    assert shift_label(label("Bm"), 1) == label("Cm")
    assert shift_label(-1, 5) == -1
    assert bar_agreement([label("C"), label("G")], [label("A"), label("E")], 3) == 1.0
    assert bar_agreement([label("C"), label("G")], [label("C")]) == 0.5      # different slot counts
    score, per = window_agreement([[label("C"), -1], [label("F"), label("F")]], [[label("C"), label("C")],
                                                                              [label("G"), label("G")]])
    assert per == [1.0, 0.0] and score == 0.5


def test_bar_sequence_loops_last_period():
    assert bar_sequence(5, 3, 10, 4) == [5, 6, 7]
    assert bar_sequence(8, 6, 10, 4) == [8, 9, 6, 7, 8, 9]
    assert bar_sequence(12, 2, 10, 4) == [8, 9]
    assert bar_sequence(0, 0, 10, 4) == []


def test_loop_period_finds_repetition():
    bars = [[label(a), label(a)] for a in ["C", "G", "Am", "F"] * 4]
    assert loop_period(bars, phrase_bars=8) in (4, 8, 16) and loop_period(bars, phrase_bars=8) % 4 == 0


def test_track_half_bar_chords_from_chroma():
    t = make_track(AXIS * 2)
    assert t.n_bars == 8
    assert t.bar_chords(0) == [label("C"), label("C")]
    assert t.bar_chords(2) == [label("Am"), label("Am")]
    assert t.bar_times(1)[0] == pytest.approx(0.3 + 4 * 0.5)


def test_regrid_and_phase_shift():
    t = make_track(AXIS * 2, bpm=60)
    d = t.regrid(2)
    assert d.bpm == pytest.approx(120) and d.n_bars == 2 * t.n_bars
    assert d.bar_chords(4) == t.bar_chords(2)               # bar 2 of the song is bar 4 when counted twice
    h = t.regrid(0.5)
    assert h.bpm == pytest.approx(30) and h.n_bars == t.n_bars // 2
    s = t.shift_phase(2)
    assert s.bar_start(0) == pytest.approx(t.beats[2]) and s.n_bars == t.n_bars - 1


def test_hop_finds_transposition_and_offset():
    # A: axis in C. B: the same loop in Eb, rotated by one bar (starts on V), vocal throughout.
    a = make_track(AXIS * 4, key=(0, "major"), work_id="A")
    rot = AXIS[2:] + AXIS[:2]
    b = make_track(rot * 4, key=(3, "major"), transpose=3, work_id="B")
    plan = plan_chain([a, b], co_seconds=16, morph_bars=2, full_bars=8, final_bars=4, half_bar=False)
    h = plan.hops[0]
    assert h.shift == -3 and h.key_shift == -3
    assert h.chord_match == pytest.approx(1.0)
    assert h.ok and h.bars == 8                              # 16 s at 2 s per bar
    # B's window starts on a bar whose chord is the one A's backing plays there
    first_a = plan.bars[h.out_start].inst[1]
    assert shift_label(b.bar_chords(h.b0)[0], h.shift) == a.bar_chords(first_a)[0]


def test_half_bar_rotation_needs_mid_bar_entry():
    # A changes chord every half bar (C G | Am F), B the same progression rotated by a half bar.
    a_seq = ["C", "G", "Am", "F"] * 8
    b_seq = ["G", "Am", "F", "C"] * 8
    a = make_track(a_seq, work_id="A")
    b = make_track(b_seq, work_id="B")
    whole = plan_chain([a, b], co_seconds=8, half_bar=False, full_bars=6, final_bars=4)
    half = plan_chain([a, b], co_seconds=8, half_bar=True, full_bars=6, final_bars=4)
    assert half.hops[0].chord_match == pytest.approx(1.0)
    assert half.tracks[1].meta.get("phase_shift") == 2
    assert whole.hops[0].chord_match < 1.0 or whole.hops[0].bars < half.hops[0].bars


def test_planner_segments_order_and_quantization():
    songs = [make_track(AXIS * 4, work_id=f"S{i}", bpm=120 + 4 * i, seed=i) for i in range(3)]
    plan = plan_chain(songs, co_seconds=20, morph_bars=2, full_bars=8, final_bars=8)
    kinds = [s[0] for s in plan.segments()]
    assert kinds[0] == "full" and kinds[-1] == "full"
    core = [k for k in kinds if k != "full"]
    assert core == ["changeover", "morph", "changeover", "morph"]
    for h in plan.hops:
        a = plan.tracks[h.a]
        assert h.bars == int(round(20 / a.bar_seconds))       # whole bars nearest 20 s
        assert all(isinstance(x, int) for x in h.a_bars)
    # segments tile the plan's bars, each bar is one OutBar
    segs = plan.segments()
    assert segs[0][1] == 0 and segs[-1][2] == len(plan.bars)
    assert all(x[2] == y[1] for x, y in zip(segs, segs[1:]))
    # the morph plays the bars right after the vocal window (the vocal lands on its own backing)
    for h in plan.hops:
        m = [ob for ob in plan.bars if ob.kind == "morph" and ob.inst[0] == h.b]
        assert m[0].inst[1] == h.b0 + h.bars
        assert all(isinstance(ob, OutBar) and ob.vocal == ob.inst for ob in m)
    # the root song opens for about full_bars
    assert 6 <= segs[0][2] - segs[0][1] <= 10


def test_changeover_shortened_until_it_matches():
    a = make_track(AXIS * 4, work_id="A")
    # B matches A for only 4 bars, then wanders
    b_seq = AXIS + ["D", "D", "E", "E", "Bb", "Bb", "Eb", "Eb"] * 3
    b = make_track(b_seq, work_id="B")
    plan = plan_chain([a, b], co_seconds=16, min_match=0.75, full_bars=8, final_bars=4, half_bar=False)
    h = plan.hops[0]
    assert h.ok and h.chord_match >= 0.75
    assert h.target_bars == 8 and h.bars < 8


def test_vocal_window_needs_singing():
    a = make_track(AXIS * 4, work_id="A")
    voc = [0.0] * 8 + [1.0] * 8
    b = make_track(AXIS * 4, work_id="B", vocal=voc)
    plan = plan_chain([a, b], co_seconds=8, full_bars=8, final_bars=4, half_bar=False)
    h = plan.hops[0]
    assert h.b0 >= 6 and h.cover >= chain.MIN_COVER


def test_tempo_octave_regrid_chosen():
    a = make_track(AXIS * 4, bpm=128, work_id="A")
    b = make_track(AXIS * 4, bpm=66, work_id="B")          # counted at half A's tempo
    plan = plan_chain([a, b], co_seconds=16, full_bars=8, final_bars=4)
    assert plan.tracks[1].factor in (2.0, 0.5) or abs(np.log2(plan.tracks[0].bpm / plan.tracks[1].bpm)) < 0.6
    assert 0.66 <= plan.tracks[0].bpm / plan.tracks[1].bpm <= 1.5
