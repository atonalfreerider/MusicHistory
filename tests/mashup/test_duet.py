"""Duet-loop planner arithmetic on synthetic songs: the cycle of pairs, every vocal covering
exactly its two pairs, two vocals everywhere except inside handoffs, the borrowed
instrumental (including the handoff into the root), the wrap, and chord matching."""

from __future__ import annotations

import pytest

from mashup_helpers import AXIS, make_track
from musichistory.mashup import duet
from musichistory.mashup.duet import (FULL, RHYTHM, handoff_roles, jump_quality, loop_sequence, pair_songs,
                                      plan_duet, window_start)


def songs(n: int, **kw):
    keys = [(0, "major"), (2, "major"), (7, "major"), (5, "major")]
    bpms = [120, 110, 126, 116]
    return [make_track(AXIS * 3, bpm=bpms[j], key=keys[j], transpose=keys[j][0], work_id=f"S{j}", **kw)
            for j in range(n)]


@pytest.fixture(scope="module", params=[3, 4])
def plan(request):
    return plan_duet(songs(request.param), pair_seconds=16, handoff_bars=2, half_bar=False)


def test_cycle_order():
    assert [pair_songs(4, k) for k in range(4)] == [(0, 1), (1, 2), (2, 3), (3, 0)]
    assert [pair_songs(3, k) for k in range(4)] == [(0, 1), (1, 2), (2, 0), (0, 1)]


def test_handoff_roles_and_borrowed_instrumental():
    # into pair m: staying S_m, entering S_(m+1), leaving S_(m-1); the entering song's instrumental,
    # the leaving song's when the entering song is the root
    assert handoff_roles(3, 1) == (1, 2, 0, 2)
    assert handoff_roles(3, 2) == (2, 0, 1, 1)          # entering S0 -> borrow the leaving song
    assert handoff_roles(3, 0) == (0, 1, 2, 1)
    assert handoff_roles(4, 3) == (3, 0, 2, 2)
    for n in (3, 4, 5):
        for m in range(n):
            staying, entering, leaving, borrowed = handoff_roles(n, m)
            assert borrowed != 0 and borrowed in (entering, leaving)
            assert len({staying, entering, leaving}) == 3


def test_loop_sequence_and_jump_quality():
    assert loop_sequence(2, 7, (4, 8)) == [2, 3, 4, 5, 6, 7, 4]
    assert loop_sequence(5, 6, (4, 8)) == [5, 6, 7, 4, 5, 6]
    own = [[1, 1], [2, 2], [3, 3], [4, 4], [1, 1], [2, 2], [3, 3], [4, 4]]
    assert jump_quality(own, (4, 8)) == 1.0             # bar 7 sounds like bar 3, the bar before the loop
    assert jump_quality(own, (0, 4)) == 1.0             # bar 4 (after the end) sounds like bar 0
    assert jump_quality(own, (1, 4)) == 0.0             # bar 3 does not sound like bar 0


def test_runs_are_contiguous_and_alternate(plan):
    n, P, H, N = plan.n_songs, plan.pair_bars, plan.handoff_bars, plan.n_bars
    assert N == n * P and len(plan.bed) == N
    assert plan.runs[0].start == 0 and plan.runs[-1].end == N
    for a, b in zip(plan.runs, plan.runs[1:]):
        assert a.end == b.start
    assert [r.kind for r in plan.runs] == ["duet", "handoff"] * n
    for k in range(n):
        d, h = plan.runs[2 * k], plan.runs[2 * k + 1]
        assert (d.pair, d.start, d.bars, d.song) == (k, k * P, P - H, 0)
        assert d.vocals == pair_songs(n, k) and d.entering is None and d.leaving is None
        assert d.src_bars == tuple(plan.bed[d.start:d.end])
        m = (k + 1) % n
        staying, entering, leaving, borrowed = handoff_roles(n, m)
        assert (h.pair, h.start, h.bars, h.song) == (m, (k + 1) * P - H, H, borrowed)
        assert h.vocals == (staying, entering) and h.entering == entering and h.leaving == leaving


def test_each_vocal_covers_exactly_its_two_pairs(plan):
    n, P, H, N = plan.n_songs, plan.pair_bars, plan.handoff_bars, plan.n_bars
    for j, v in enumerate(plan.voices):
        assert v.start == window_start(j, P, H) and len(v.src_bars) == 2 * P + H
        # full (no fade) through the duets of pairs j-1 and j and the handoff between them
        full = {o % N for o in range(v.start + H, v.end - H)}
        duets = {o for r in plan.runs if r.kind == "duet" and j in r.vocals for o in range(r.start, r.end)}
        assert duets <= full
        assert len([r for r in plan.runs if r.kind == "duet" and j in r.vocals]) == 2
        # half-gain points: H/2 into its first handoff .. H/2 into its last = exactly two pairs
        assert (v.end - H / 2) - (v.start + H / 2) == 2 * P


def test_two_vocals_everywhere_three_only_in_handoffs(plan):
    n, N = plan.n_songs, plan.n_bars
    for o in range(N):
        r = plan.run_at(o)
        sounding = plan.voices_at(o)
        if r.kind == "duet":
            assert sorted(sounding) == sorted(pair_songs(n, r.pair))
        else:
            assert sorted(sounding) == sorted({r.vocals[0], r.entering, r.leaving})
            assert len(sounding) == 3


def test_wrap_runs_from_last_pair_into_the_first(plan):
    n, P, H, N = plan.n_songs, plan.pair_bars, plan.handoff_bars, plan.n_bars
    last = plan.runs[-1]
    assert last.kind == "handoff" and last.pair == 0 and last.end == N
    assert last.leaving == n - 1 and last.entering == 1 and last.vocals == (0, 1)
    # the root's window starts in the last pair and runs on, across the wrap, through pair 0
    v0 = plan.voices[0]
    assert v0.start < 0 and v0.end == P
    assert plan.voice_offset(0, N - 1) is not None and plan.voice_offset(0, 0) is not None
    assert plan.voice_offset(0, N - 1) + 1 == plan.voice_offset(0, 0)
    # song 1 enters in the handoff that ends the file
    assert plan.voice_offset(1, N - H) == 0 and plan.voice_offset(1, 0) == H


def test_windows_are_consecutive_bars_or_loop_back(plan):
    for v in plan.voices:
        tr = plan.tracks[v.song]
        for a, b in zip(v.src_bars, v.src_bars[1:]):
            assert b == a + 1 or (v.loop is not None and a == v.loop[1] - 1 and b == v.loop[0])
            assert 0 <= b < tr.n_bars


def test_same_progression_matches_in_key_derived_shift(plan):
    for j, v in enumerate(plan.voices):
        assert v.shift == v.key_shift == (0 if j == 0 else duet.wrap(-plan.tracks[j].frame_shift))
        assert v.chord_match == pytest.approx(1.0)
    for r in plan.runs:
        assert r.chord_match == pytest.approx(1.0) and r.stems == FULL


def test_borrowed_bars_agree_with_the_bed():
    p = plan_duet(songs(3), pair_seconds=16, handoff_bars=2, half_bar=False)
    for r in p.runs:
        if r.kind == "handoff":
            for o in range(r.start, r.end):
                song, src, shift = p.inst_at(o)
                assert song == r.song
                assert duet.heard(p.tracks[song].bar_chords(src), shift) == p.tracks[0].bar_chords(p.bed[o])


def test_instrumental_lead_thins_the_instrumental_under_it():
    s = songs(4)
    s[2] = make_track(AXIS * 3, bpm=126, key=(7, "major"), transpose=7, work_id="S2", vocal=[0.0] * 12)
    s[2].lead = "other"
    p = plan_duet(s, pair_seconds=16, handoff_bars=2, half_bar=False)
    assert p.voices[2].lead == "other"
    for r in p.runs:
        sounding = {j for o in range(r.start, r.end) for j in p.voices_at(o)}
        assert r.stems == (RHYTHM if 2 in sounding or r.song == 2 else FULL)


def test_tempo_octave_nearest_root():
    s = songs(3)
    s[1] = make_track(AXIS * 6, bpm=230, key=(2, "major"), transpose=2, work_id="S1")
    p = plan_duet(s, pair_seconds=16, handoff_bars=2, half_bar=False)
    assert p.tracks[1].factor == 0.5 and p.tracks[1].bpm == pytest.approx(115, rel=0.01)


def test_pair_length_in_whole_bars_and_errors():
    p = plan_duet(songs(3), pair_seconds=20, handoff_bars=2, half_bar=False)
    assert p.pair_bars == 10                            # 20 s of 2 s bars
    assert plan_duet(songs(3), pair_seconds=1, handoff_bars=2, half_bar=False).pair_bars == 4
    with pytest.raises(ValueError):
        plan_duet(songs(2))
    with pytest.raises(ValueError):
        plan_duet(songs(3), handoff_bars=0)


def test_duets_stage_registered_with_defaults():
    import argparse
    import importlib

    from musichistory import cli

    module_name, help_text = cli.STAGES["duets"]
    assert module_name == "musichistory.mashup.duet_stage" and help_text
    mod = importlib.import_module(module_name)
    p = argparse.ArgumentParser()
    mod.add_arguments(p)
    args = p.parse_args([])
    assert (args.pair, args.handoff_bars, args.no_verify, args.path) == (20.0, 2, False, None)
    args = p.parse_args(["--path", "a", "--path", "b", "--pair", "16", "--handoff-bars", "4", "--no-verify"])
    assert (args.path, args.pair, args.handoff_bars, args.no_verify) == (["a", "b"], 16.0, 4, True)


def test_bed_stays_in_the_root_key():
    # the root's preview ends on an off-key section (a semitone down): the bed loops its in-key bars
    root = make_track(AXIS * 2 + ["B", "B", "F#", "F#", "Abm", "Abm", "E", "E"] * 2, bpm=120, work_id="S0")
    s = songs(3)
    p = plan_duet([root, *s[1:]], pair_seconds=16, handoff_bars=2, half_bar=False)
    assert p.params["bed_fit"] == 1.0
    assert all(b < 8 for b in p.bed)
    lo, hi = p.params["bed_loop"]
    assert hi <= 8 and hi - lo in duet.LOOP_CANDIDATES


def test_root_with_instrumental_lead_splits_its_other_stem():
    s = songs(3)
    s[0] = make_track(AXIS * 3, bpm=120, work_id="S0", vocal=[0.0] * 12)
    s[0].lead = "other"
    p = plan_duet(s, pair_seconds=16, handoff_bars=2, half_bar=False)
    assert p.voices[0].stem == duet.ROOT_OTHER_LEAD and p.voices[1].stem == "vocals"
    for r in p.runs:
        assert r.stems == (duet.ROOT_SPLIT if r.song == 0 else FULL)


def test_slow_root_counted_twice_as_fast():
    s = songs(3)
    s[0] = make_track(AXIS * 3, bpm=62, work_id="S0")
    p = plan_duet(s, pair_seconds=16, handoff_bars=2, half_bar=False)
    assert p.tracks[0].factor in (1.0, 2.0)
    assert {r.factor for r in duet.root_countings(s)[:2]} == {1.0, 2.0}
