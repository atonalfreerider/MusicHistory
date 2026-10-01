"""Seconds for a plan: the mix beat grid, the morph's tempo glide, the time maps of every
piece (vocal beats land on the backing's beats), transposition curves and stem envelopes."""

from __future__ import annotations

import numpy as np
import pytest

from mashup_helpers import AXIS, make_track
from musichistory.mashup import timeline
from musichistory.mashup.chain import plan_chain


@pytest.fixture(scope="module")
def tl():
    a = make_track(AXIS * 4, bpm=120, work_id="A", jitter=0.004, seed=1)
    b = make_track(AXIS * 4, bpm=108, work_id="B", key=(2, "major"), transpose=2, jitter=0.004, seed=2)
    c = make_track(AXIS * 4, bpm=115, work_id="C", key=(7, "major"), transpose=7, jitter=0.004, seed=3)
    plan = plan_chain([a, b, c], co_seconds=12, morph_bars=2, full_bars=6, final_bars=4, half_bar=False)
    return timeline.build(plan)


def test_grid_native_sections_use_backing_beats(tl):
    plan, bpb = tl.plan, tl.bpb
    assert tl.grid[0] == 0.0 and np.all(np.diff(tl.grid) > 0)
    assert len(tl.grid) == len(plan.bars) * bpb + 1
    for o, ob in enumerate(plan.bars):
        if ob.kind == "morph":
            continue
        src = np.diff(plan.tracks[ob.inst[0]].bar_times(ob.inst[1]))
        assert np.allclose(np.diff(tl.grid[o * bpb:(o + 1) * bpb + 1]), src)


def test_morph_glides_from_previous_tempo_to_own(tl):
    plan, bpb = tl.plan, tl.bpb
    for h in plan.hops:
        m = [o for o, ob in enumerate(plan.bars) if ob.kind == "morph" and ob.inst[0] == h.b]
        ibis = np.diff(tl.grid[m[0] * bpb:(m[-1] + 1) * bpb + 1])
        src = np.concatenate([np.diff(plan.tracks[h.b].bar_times(plan.bars[o].inst[1])) for o in m])
        ratio = ibis / src
        before = np.median(np.diff(tl.grid[(m[0] - 1) * bpb:m[0] * bpb + 1]))
        r0 = before / np.median(src)
        assert ratio[0] == pytest.approx(r0, rel=0.08)
        assert ratio[-1] == pytest.approx(1.0, abs=0.01 + abs(r0 - 1) * 0.05)
        assert np.all(np.diff(ratio) * np.sign(1 - r0) >= -1e-9)          # monotonic toward 1


def test_vocal_beats_map_onto_backing_beats(tl):
    plan, bpb = tl.plan, tl.bpb
    for h in plan.hops:
        b = plan.tracks[h.b]
        for k in range(h.bars):
            src = b.bar_times(h.b0 + k)
            out = tl.grid[(h.out_start + k) * bpb:(h.out_start + k + 1) * bpb + 1]
            got = [tl.mix_to_source(h.b, t)[0] for t in out[:-1]]
            assert np.allclose(got, src[:-1], atol=1e-6)


def test_pieces_native_and_warped(tl):
    plan = tl.plan
    for p in tl.pieces:
        slopes = np.diff(p.out_times) / np.diff(p.src_times)
        if not p.warped:
            assert np.allclose(slopes, 1.0)
        bars = [plan.bars[o] for o in p.out_bars]
        if any(ob.kind == "changeover" and ob.vocal[0] == p.song for ob in bars):
            assert p.warped
    s0 = [p for p in tl.pieces if p.song == 0]
    assert s0 and not any(p.warped for p in s0)                          # the root song is never stretched


def test_semitones_curve(tl):
    for h in tl.plan.hops:
        m0, m1 = tl.morphs[h.b]
        total = h.shift + tl.detune.get(h.b, 0.0)
        assert tl.semitones(h.b, m0 - 1.0) == pytest.approx(total)
        assert tl.semitones(h.b, (m0 + m1) / 2) == pytest.approx(total / 2)
        assert tl.semitones(h.b, m1 + 0.01) == 0.0
    assert tl.semitones(0, 5.0) == 0.0


def test_envelopes_cover_roles(tl):
    plan = tl.plan
    for h in plan.hops:
        t0, _ = tl.bar_span(h.out_start)
        _, t1 = tl.bar_span(h.out_start + h.bars - 1)
        voc = tl.envelopes[(h.b, "vocals")]
        assert any(a <= t0 + 1e-9 and b >= t1 - 1e-9 for a, b in voc)
        inst_a = tl.envelopes[(h.a, "instruments")]
        assert any(a <= t0 + 1e-9 and b >= t1 - 1e-9 for a, b in inst_a)
        # the backing's own vocal is out during the changeover
        assert not any(a < t1 - 1e-9 and b > t0 + 1e-9 for a, b in tl.envelopes[(h.a, "vocals")])
    last = len(plan.tracks) - 1
    assert tl.envelopes[(last, "vocals")][-1][1] == pytest.approx(tl.seconds)


def test_instrument_lead_uses_rhythm_backing():
    a = make_track(AXIS * 4, work_id="A")
    b = make_track(AXIS * 4, work_id="B", vocal=[0.0] * 16)
    b.lead = "other"
    plan = plan_chain([a, b], co_seconds=8, full_bars=6, final_bars=4, half_bar=False)
    t = timeline.build(plan)
    assert (1, "other") in t.envelopes and (0, "drums") in t.envelopes and (0, "bass") in t.envelopes
