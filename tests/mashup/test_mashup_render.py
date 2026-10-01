"""Rendering on synthetic stems: native pieces are copied sample-exactly, a warped vocal's
clicks land on the backing's beats, envelopes fade with equal power, the limiter holds its
ceiling, and the joins are click-free."""

from __future__ import annotations

import numpy as np
import pytest

from mashup_helpers import AXIS, make_track
from musichistory.mashup import render, timeline, verify
from musichistory.mashup.chain import plan_chain

soundfile = pytest.importorskip("soundfile")
pytest.importorskip("pylibrb")
SR = render.SR


def test_crop_map_and_extend():
    src = np.array([1.0, 2.0, 3.0])
    dst = np.array([0.0, 1.2, 2.4])
    m = render.crop_map(src, dst, 0.6, 1.8)
    assert m.dst[0] == pytest.approx(0.6) and m.src[0] == pytest.approx(1.5)
    assert m.dst[-1] == pytest.approx(1.8) and m.src[-1] == pytest.approx(2.5)
    s, d = render.extend(src, dst, 0.5, 3.2)
    assert s[0] == pytest.approx(0.5) and d[0] == pytest.approx(-0.6)
    assert s[-1] == pytest.approx(3.2) and d[-1] == pytest.approx(2.64)


def test_limiter_holds_ceiling():
    t = np.arange(SR * 3) / SR
    y = np.stack([0.4 * np.sin(2 * np.pi * 220 * t)] * 2, axis=1).astype(np.float32)
    y[SR:SR + 3000] *= 3.0
    z = render.limit(y, SR, 0.7)
    assert np.abs(z).max() <= 0.7 + 1e-3
    assert np.abs(z[2 * SR:]).max() == pytest.approx(0.4, abs=0.01)       # recovers
    assert np.abs(z[: SR - 2000]).max() == pytest.approx(0.4, abs=0.01)   # untouched before


def click_train(beats, n, sr=SR, freq=1500.0):
    y = np.zeros(n, np.float32)
    burst = (np.hanning(300) * np.sin(2 * np.pi * freq * np.arange(300) / sr)).astype(np.float32)
    for b in beats:
        i = int(b * sr)
        if i + 300 < n:
            y[i:i + 300] += burst
    return y


@pytest.fixture(scope="module")
def chain_audio(tmp_path_factory):
    root = tmp_path_factory.mktemp("stems")
    a = make_track(AXIS * 4, bpm=120, work_id="A", jitter=0.003, seed=4)
    b = make_track(AXIS * 4, bpm=104, work_id="B", jitter=0.003, seed=5)
    dirs = {}
    for j, tr in enumerate((a, b)):
        d = root / tr.work_id
        d.mkdir()
        n = int((tr.beats[-1] + 1.0) * SR)
        t = np.arange(n) / SR
        drums = click_train(tr.beats, n, freq=4000.0)
        voc = click_train(tr.beats, n, freq=900.0) * 0.8 + 0.05 * np.sin(2 * np.pi * 330 * t).astype(np.float32)
        bass = 0.1 * np.sin(2 * np.pi * 55 * t).astype(np.float32)
        other = 0.05 * np.sin(2 * np.pi * 262 * t).astype(np.float32)
        for name, sig in (("vocals", voc), ("drums", drums), ("bass", bass), ("other", other),
                          ("instruments", drums + bass + other)):
            soundfile.write(str(d / f"{name}.wav"), np.stack([sig, sig], axis=1), SR, subtype="FLOAT")
        dirs[j] = d
    plan = plan_chain([a, b], co_seconds=8, morph_bars=2, full_bars=6, final_bars=4, half_bar=False)
    tl = timeline.build(plan)
    cache = render.StemCache(dirs)
    lead, back, joins = render.render_buses(tl, cache, {0: {"mix": -18.0}, 1: {"mix": -18.0}})
    return tl, cache, lead, back, joins


def test_native_piece_is_sample_exact(chain_audio):
    tl, cache, *_ = chain_audio
    y, _ = render.render_stem(tl, 0, "instruments", cache.get(0, "instruments"))
    src = cache.get(0, "instruments")
    p = [q for q in tl.song_pieces(0) if not q.warped][0]
    i_out = int(round(p.out_times[1] * SR))
    i_src = int(round(p.src_times[1] * SR))
    seg = slice(i_out, i_out + SR // 2)
    assert np.allclose(y[seg], src[i_src:i_src + SR // 2], atol=1e-6)


def test_warped_vocal_lands_on_backing_beats(chain_audio):
    tl, cache, lead, back, _ = chain_audio
    h = tl.plan.hops[0]
    t0, _ = tl.bar_span(h.out_start)
    _, t1 = tl.bar_span(h.out_start + h.bars - 1)
    grid = tl.grid[(tl.grid >= t0 + 0.3) & (tl.grid < t1 - 0.3)]
    from scipy.signal import butter, sosfiltfilt

    env = np.abs(sosfiltfilt(butter(4, [700, 1100], "bp", fs=SR, output="sos"), lead[:, 0]))
    errs = []
    for g in grid:
        i = int(g * SR)
        w = env[i - 2000:i + 2000]
        errs.append((np.argmax(w > 0.3 * w.max()) - 2000) / SR)
    errs = np.array(errs)
    assert len(errs) >= 8
    assert np.median(np.abs(errs)) < 0.008


def test_envelope_equal_power_fades(chain_audio):
    tl, *_ = chain_audio
    h = tl.plan.hops[0]
    t0, _ = tl.bar_span(h.out_start)
    n = int(np.ceil(tl.seconds * SR)) + 1
    out_ = render.envelope(tl, tl.envelopes[(0, "vocals")], n)
    in_ = render.envelope(tl, tl.envelopes[(1, "vocals")], n)
    i = int(t0 * SR)
    assert out_[i - SR] == 1.0 and out_[i + SR] == 0.0
    assert in_[i - SR] == 0.0 and in_[i + SR] == 1.0
    f = render.fade_len(tl, t0)
    sl = slice(int((t0 - f / 2) * SR) + 2, int((t0 + f / 2) * SR) - 2)
    assert np.allclose(out_[sl] ** 2 + in_[sl] ** 2, 1.0, atol=0.02)


def test_mix_has_no_clicks_and_no_nan(chain_audio):
    tl, _, lead, back, joins = chain_audio
    mix = render.master(lead, back, tl)
    assert np.isfinite(mix).all()
    bounds = sorted(set(joins) | {a for ivs in tl.envelopes.values() for iv in ivs for a in iv
                                  if 0.5 < a < tl.seconds - 0.5})
    res = verify.clicks(mix, SR, bounds, tl.grid, tl.bpb)
    assert res["clicks"] == [], res
    scaled = mix * (0.5 / np.abs(mix).max())
    assert verify.peak(scaled) == {"peak_dbfs": pytest.approx(-6.02, abs=0.01), "full_scale_samples": 0}
    # a real discontinuity is caught
    t = bounds[0]
    bad = mix.copy()
    bad[int(t * SR):] += 1.5
    assert verify.clicks(bad, SR, [t], tl.grid, tl.bpb)["clicks"] == [round(t, 3)]
