"""Rendering a mosaic on synthetic stems: whole loops and sections, pieces landing on the target's
beats at their planned times, equal-power cuts, seamless loop tiles and no clicks at any join."""

from __future__ import annotations

import numpy as np
import pytest

from mosaic_helpers import ROOT  # noqa: F401
from musichistory.mashup import verify as mverify
from musichistory.mosaic import render

pytest.importorskip("pylibrb")
SR = render.SR


def tone(n: int, hz: float, amp: float = 0.2) -> np.ndarray:
    return (amp * np.sin(2 * np.pi * hz * np.arange(n) / SR)).astype(np.float32)


def notes_signal(n: int, beats: np.ndarray, hz: float, every: int = 1, amp: float = 0.3) -> np.ndarray:
    """A 'voice': a tone sung for 0.8 of every ``every``-th beat, soft attack and release."""
    y = np.zeros(n, np.float32)
    for k in range(0, len(beats) - 1, every):
        a, b = int(beats[k] * SR), int((beats[k] + 0.8 * (beats[k + 1] - beats[k])) * SR)
        if b >= n:
            break
        env = np.sin(np.pi * np.linspace(0, 1, b - a)) ** 0.3
        y[a:b] += (amp * env * np.sin(2 * np.pi * hz * np.arange(a, b) / SR)).astype(np.float32)
    return y


class FakeStems:
    def __init__(self, tracks: dict[tuple[str, str], np.ndarray]):
        self.tracks = tracks

    def get(self, work_id: str, stem: str) -> np.ndarray:
        y = self.tracks[(work_id, stem)]
        return np.stack([y, y], axis=1)


@pytest.fixture(scope="module")
def rendered():
    dur = 40.0
    n = int(dur * SR)
    t_beats = 0.25 + np.arange(int((dur - 1) / 0.5)) * 0.5            # target: 120 BPM
    s_beats = 0.4 + np.arange(int((dur - 1) / 0.55)) * 0.55            # source A: ~109 BPM
    b_beats = 0.1 + np.arange(int((dur - 1) / 0.25)) * 0.25            # source B: counted at 240
    clicks = np.zeros(n, np.float32)
    for b in t_beats:
        i = int(b * SR)
        clicks[i:i + 200] += (np.hanning(200) * np.sin(2 * np.pi * 3000 * np.arange(200) / SR)).astype(np.float32)
    stems = FakeStems({
        ("T", "instruments"): clicks * 0.5 + tone(n, 110.0, 0.1),
        ("T", "vocals"): notes_signal(n, t_beats, 440.0),
        ("A", "vocals"): notes_signal(n, s_beats, 330.0),
        ("B", "vocals"): notes_signal(n, b_beats, 392.0, every=2),
        ("C", "vocals"): notes_signal(n, s_beats, 523.25),
    })
    pieces = [render.PieceSpec("A", s_beats, 0.0, 1.0, -10.0, 2, first=1.0, last=6.8, duration=dur),
              render.PieceSpec("B", b_beats, 0.1, 0.5, -1.0, -3, first=9.0, last=14.8, duration=dur)]
    voices = [render.VoiceSpec("C", s_beats, 0.0, 1.0, 4.0, 3, duration=dur)]
    spec = render.Spec("T", t_beats, beat0=4, n_beats=16, bpb=4, tuning=0.0, levels={"mix": -18.0},
                       pieces=pieces, voices=voices, max_seconds=40.0)
    return spec, render.render(spec, stems)


def test_whole_loops_and_sections(rendered):
    spec, r = rendered
    assert abs(r.grid.seconds - 8.0) < 1e-9 and r.n_loops == 5
    assert len(r.mix) == 5 * int(round(8.0 * SR))
    assert r.sections == [("original", 0, 1), ("mosaic", 1, 2), ("harmony", 3, 2)]
    assert render.sections(3) == [("original", 0, 1), ("mosaic", 1, 1), ("harmony", 2, 1)]
    assert [k for k, _, _ in render.sections(11)] == ["original", "mosaic", "harmony"]
    assert sum(n for _, _, n in render.sections(11)) == 11
    with pytest.raises(ValueError):
        render.sections(2)


def rms(y: np.ndarray) -> float:
    return float(np.sqrt(np.mean(y.astype(np.float64) ** 2))) if len(y) else 0.0


def test_buses_sound_only_in_their_sections(rendered):
    spec, r = rendered
    T = r.grid.seconds
    seg = lambda bus, k: r.buses[bus][int((k * T + 0.5) * SR):int(((k + 1) * T - 0.5) * SR)]
    assert rms(seg("target_vocal", 0)) > 0.02 and rms(seg("target_vocal", 3)) > 0.02
    assert rms(seg("target_vocal", 1)) < 1e-4 and rms(seg("target_vocal", 2)) < 1e-4
    assert rms(seg("harmony_0", 0)) < 1e-4 and rms(seg("harmony_0", 4)) > 0.01
    assert rms(seg("pieces", 0)) < 1e-4 and rms(seg("pieces", 1)) > 0.01 and rms(seg("pieces", 3)) < 1e-4
    assert rms(seg("bed", 2)) > 0.01


def test_pieces_land_on_the_target_beats(rendered):
    spec, r = rendered
    T = r.grid.seconds
    y = r.buses["pieces"][:, 0]
    # piece A's notes are its own beats moved by the time map: source beat k -> loop beat k - 10
    for k in range(11, 17):
        t = T + float(r.grid.time(k - 10))                  # mosaic loop 1
        a = int((t + 0.05) * SR)
        if t > T + r.cuts[0][1] - 0.1:
            break
        assert rms(y[a:a + int(0.15 * SR)]) > 0.02, k        # a sung note just after each beat
        assert rms(y[int((t - 0.07) * SR):int((t - 0.02) * SR)]) < rms(y[a:a + int(0.15 * SR)])


def test_piece_cuts_share_a_point_when_close():
    cuts = render.piece_cuts([(0.1, 3.0), (3.1, 5.0), (7.0, 7.95)], 8.0)
    assert cuts[0][1] == cuts[1][0] and 3.0 < cuts[0][1] < 3.1
    assert cuts[1][1] == pytest.approx(5.0 + render.TAIL) and cuts[2][0] == pytest.approx(7.0 - render.LEAD)
    # around the loop: the last piece meets the first one's onset in the next loop
    assert cuts[2][1] == pytest.approx(cuts[0][0] + 8.0)
    assert 7.95 < cuts[2][1] < 8.1
    # far apart: each piece keeps its lead and tail
    far = render.piece_cuts([(1.0, 2.0), (5.0, 6.0)], 8.0)
    assert far == [pytest.approx((1.0 - render.LEAD, 2.0 + render.TAIL)), pytest.approx((5.0 - render.LEAD, 6.0 + render.TAIL))]


def test_window_is_equal_power_at_a_shared_cut():
    n, c, xf = 4000, 2000.0, 400
    a = render.window(n, 0.0, c, xf)
    b = render.window(n, c, n * 2.0, xf)
    s = a ** 2 + b ** 2
    assert np.allclose(s[300:3500], 1.0, atol=1e-3)


def test_tiles_are_seamless_and_joins_click_free(rendered):
    spec, r = rendered
    grid = np.array([t for t, _ in render.mix_beats(r)])
    res = mverify.clicks(r.mix, SR, r.joins, grid, spec.bpb)
    assert res["checked"] >= 8 and res["clicks"] == [], res
    # the bed repeats exactly every loop: the tile is identical, so is the mix bed
    n_tile = int(round(r.grid.seconds * SR))
    assert np.array_equal(r.buses["bed"][:n_tile], r.buses["bed"][n_tile:2 * n_tile])
    # no step at the loop boundary larger than the signal's own steps
    bed = r.buses["bed"][:, 0]
    step = np.abs(np.diff(bed))
    assert step[n_tile - 5:n_tile + 5].max() <= 1.5 * np.percentile(step, 99.9)


def test_mix_beats_follow_the_loop(rendered):
    spec, r = rendered
    mb = render.mix_beats(r)
    assert len(mb) == r.n_loops * spec.n_beats
    assert [q for _, q in mb[:17]] == [float(q) for q in range(16)] + [0.0]
    assert all(b[0] > a[0] for a, b in zip(mb, mb[1:]))
