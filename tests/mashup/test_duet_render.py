"""Rendering a duet loop on synthetic stems: the loop is seamless across its wrap, every warped
vocal lands on the bed's beats, two vocals sound in every duet bar (three in handoffs, with
equal-power crossfades), the instrumental never drops out, and the circular helpers wrap."""

from __future__ import annotations

import numpy as np
import pytest

from mashup_helpers import AXIS, make_track
from musichistory.mashup import duet_render, verify
from musichistory.mashup.duet import plan_duet

soundfile = pytest.importorskip("soundfile")
pytest.importorskip("pylibrb")
SR = duet_render.SR
VOICE_HZ = (700.0, 1000.0, 1400.0)


def click_train(beats, n, freq, sr=SR, length=300):
    y = np.zeros(n, np.float32)
    burst = (np.hanning(length) * np.sin(2 * np.pi * freq * np.arange(length) / sr)).astype(np.float32)
    for b in beats:
        i = int(b * sr)
        if 0 <= i and i + length < n:
            y[i:i + length] += burst
    return y


@pytest.fixture(scope="module")
def loop(tmp_path_factory):
    root = tmp_path_factory.mktemp("duet_stems")
    keys = [(0, "major"), (2, "major"), (7, "major")]
    tracks = [make_track(AXIS * 2, bpm=[120, 110, 126][j], key=keys[j], transpose=keys[j][0], work_id=f"S{j}",
                         jitter=0.003, seed=10 + j) for j in range(3)]
    dirs = {}
    for j, tr in enumerate(tracks):
        d = root / tr.work_id
        d.mkdir()
        n = int((tr.beats[-1] + 1.0) * SR)
        t = np.arange(n) / SR
        voc = click_train(tr.beats, n, VOICE_HZ[j]) * 0.8 + 0.03 * np.sin(2 * np.pi * 330 * t).astype(np.float32)
        drums = click_train(tr.beats, n, 4000.0, length=200)
        bass = 0.1 * np.sin(2 * np.pi * 55 * t).astype(np.float32)
        other = 0.05 * np.sin(2 * np.pi * 262 * t).astype(np.float32)
        for name, sig in (("vocals", voc), ("drums", drums), ("bass", bass), ("other", other),
                          ("instruments", drums + bass + other)):
            soundfile.write(str(d / f"{name}.wav"), np.stack([sig, sig], axis=1), SR, subtype="FLOAT")
        dirs[j] = d
    plan = plan_duet(tracks, pair_seconds=8, handoff_bars=2, half_bar=False)
    tl = duet_render.build(plan)
    stems = duet_render.DuetStems(dirs)
    buses = duet_render.render_buses(tl, stems, {j: {"mix": -18.0} for j in range(3)})
    return tl, buses


def test_add_circular_wraps():
    buf = np.zeros((10, 2), np.float32)
    duet_render.add_circular(buf, np.ones((4, 2), np.float32), 8)
    duet_render.add_circular(buf, np.ones((3, 2), np.float32), -1)
    assert buf[:, 0].tolist() == [2, 2, 0, 0, 0, 0, 0, 0, 1, 2]


def test_limiter_is_circular():
    t = np.arange(SR * 2) / SR
    y = np.stack([0.4 * np.sin(2 * np.pi * 220 * t)] * 2, axis=1).astype(np.float32)
    y[:2000] *= 3.0                                         # a peak right after the wrap
    z = duet_render.limit_circular(y, SR, 0.7)
    assert np.abs(z).max() <= 0.7 + 1e-3
    # the gain already ramps down before the wrap (at the end of the file), as it does before the peak
    assert np.abs(z[-50:]).max() < 0.4


def test_loop_length_and_plan(loop):
    tl, buses = loop
    assert tl.plan.pair_bars == 4 and tl.plan.n_bars == 12
    assert len(buses.bed) == int(round(tl.seconds * SR))
    assert np.isfinite(buses.mix()).all()


def test_loop_is_seamless(loop):
    tl, buses = loop
    mix = buses.mix()
    n = len(mix)
    half = n // 2
    rot = np.roll(mix, half, axis=0)                        # the wrap now sits in the middle
    grid = np.sort((tl.grid[:-1] + half / SR) % tl.seconds)
    t_seam = (n - half) / SR
    res = verify.clicks(rot, SR, [t_seam], grid, tl.bpb)
    assert res["clicks"] == [], res
    # the root's vocal runs across the wrap: its clicks on the last and the first beats are all there
    lead0 = buses.leads[0][:, 0]
    for q in list(range(tl.n_beats - 3, tl.n_beats)) + [0, 1, 2]:
        i = int(tl.grid[q] * SR)
        w = np.abs(lead0[max(0, i - 600):i + 900])
        assert w.max() > 0.2


def _onset_errors(y, grid, t0, t1):
    from scipy.signal import butter, sosfiltfilt

    env = np.abs(sosfiltfilt(butter(4, 300, "hp", fs=SR, output="sos"), y))
    errs = []
    for g in grid[(grid >= t0) & (grid < t1)]:
        i = int(g * SR)
        w = env[max(0, i - 2000):i + 2000]
        if len(w) < 4000 or w.max() <= 0:
            continue
        errs.append((np.argmax(w > 0.3 * w.max()) - 2000) / SR)
    return np.array(errs)


def test_warped_vocals_land_on_the_bed_beats(loop):
    tl, buses = loop
    for el in [e for e in tl.elements if e.role == "lead"]:
        a, b = el.ramp_in[1], el.ramp_out[0]                  # full-gain part (unwrapped seconds)
        y = buses.leads[el.song][:, 0]
        k = np.floor(a / tl.seconds)
        a, b = a - k * tl.seconds, b - k * tl.seconds
        if b <= tl.seconds:
            errs = _onset_errors(y, tl.grid, a + 0.3, b - 0.3)
        else:
            errs = np.r_[_onset_errors(y, tl.grid, a + 0.3, tl.seconds - 0.3),
                         _onset_errors(y, tl.grid, 0.3, b - tl.seconds - 0.3)]
        assert len(errs) >= 8
        assert np.median(np.abs(errs)) < 0.008, (el.song, errs)


def _bar_rms(y, tl, o):
    a, b = int(tl.bar_time(o) * SR), int(tl.bar_time(o + 1) * SR)
    return float(np.sqrt(np.mean(y[a:b, 0].astype(np.float64) ** 2)))


def test_two_vocals_in_every_bar_three_in_handoffs(loop):
    tl, buses = loop
    plan = tl.plan
    for o in range(plan.n_bars):
        loud = sorted(j for j, y in buses.leads.items() if _bar_rms(y, tl, o) > 0.01)
        assert loud == sorted(plan.voices_at(o)), o
        assert len(loud) == (2 if plan.run_at(o).kind == "duet" else 3)


def test_handoff_crossfades_keep_power(loop):
    tl, _ = loop
    leads = {e.song: e for e in tl.elements if e.role == "lead"}
    for r in tl.plan.runs:
        if r.kind != "handoff":
            continue
        t = np.linspace(tl.bar_time(r.start), tl.bar_time(r.end), 200)
        # elements are on the unwrapped timeline: try the loop lengths around
        def g(el):
            return np.max([duet_render.gain_at(el, t + k * tl.seconds) for k in (-1, 0, 1)], axis=0)
        gl, ge, gs = g(leads[r.leaving]), g(leads[r.entering]), g(leads[r.vocals[0]])
        assert np.allclose(gl ** 2 + ge ** 2, 1.0, atol=1e-4)
        assert gl[0] == pytest.approx(1.0, abs=0.02) and ge[-1] == pytest.approx(1.0, abs=0.02)
        assert np.allclose(gs, 1.0)
        insts = [e for e in tl.elements if e.role != "lead"]
        mid = np.array([0.5 * (t[0] + t[-1])])
        on = [e for e in insts if max(duet_render.gain_at(e, mid + k * tl.seconds)[0] for k in (-1, 0, 1)) > 0.99]
        assert len(on) == 1 and on[0].role == "borrowed" and on[0].song == r.song


def test_instrumental_never_drops_out(loop):
    tl, buses = loop
    t = np.arange(0, tl.seconds, 0.01)
    insts = [e for e in tl.elements if e.role != "lead"]
    power = sum(np.max([duet_render.gain_at(e, t + k * tl.seconds) for k in (-1, 0, 1)], axis=0) ** 2 for e in insts)
    assert np.allclose(power, 1.0, atol=1e-3)
    hop = SR // 10
    bed = buses.bed[:, 0]
    rms = np.sqrt(np.mean(bed[: len(bed) // hop * hop].reshape(-1, hop).astype(np.float64) ** 2, axis=1))
    assert rms.min() > 0.3 * np.median(rms)
