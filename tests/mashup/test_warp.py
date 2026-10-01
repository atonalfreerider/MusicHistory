"""Time-map stretching: clicks on an irregular source grid land on a regular output grid
(also with a transposition and with a gliding pitch), with no drift."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

pytest.importorskip("pylibrb")

from musichistory.mashup.warp import TimeMap, beat_map, warp  # noqa: E402

SR = 44100


def clicks(times, n, sr=SR, tone=440.0):
    y = np.zeros(n, np.float32)
    burst = (np.hanning(400)[:200] * np.sin(2 * np.pi * 1000 * np.arange(200) / sr)).astype(np.float32)
    for t in times:
        i = int(t * sr)
        y[i:i + 200] += burst
    y += (0.1 * np.sin(2 * np.pi * tone * np.arange(n) / sr)).astype(np.float32)
    return y


def onsets_near(y, times, sr=SR):
    from scipy.signal import butter, sosfiltfilt

    env = np.abs(sosfiltfilt(butter(4, [800, 1500], "bp", fs=sr, output="sos"), y))
    out = []
    for t in times:
        i = int(t * sr)
        w = env[max(0, i - 6000):i + 6000]
        if len(w) < 12000:
            continue
        out.append((np.argmax(w > 0.3 * w.max()) - 6000) / sr)
    return np.array(out)


def test_time_map_validation():
    with pytest.raises(ValueError):
        TimeMap([0, 1, 1], [0, 1, 2])
    m = beat_map([1.0, 1.5, 2.0], [0.0, 0.6, 1.2], margin=0.5, src_limit=(0.8, 10.0))
    assert m.src[0] == pytest.approx(0.8) and m.dst[0] == pytest.approx(-0.24)
    assert m.src[-1] == pytest.approx(2.5) and m.dst[-1] == pytest.approx(1.8)
    assert m.inverse(0.6) == pytest.approx(1.5)


@pytest.mark.parametrize("scale,semis", [(1.0, 0), (1.2, 3), (0.85, -4)])
def test_irregular_grid_onto_regular(scale, semis):
    src = np.cumsum(np.r_[0.5, 0.5 + 0.05 * np.sin(np.arange(30))])
    n = int((src[-1] + 0.6) * SR)
    y = clicks(src, n)
    dst = 1.0 + np.arange(len(src)) * 0.5 * scale
    m = beat_map(src, dst, margin=0.4, src_limit=(0.0, n / SR))
    seg = y[int(m.src[0] * SR):]
    out = warp(seg[:, None], SR, m, semis, formant=False)
    assert len(out) == int(round((m.dst[-1] - m.dst[0]) * SR))
    rel = dst - m.dst[0]
    err = onsets_near(out[:, 0], rel) - 0.0017      # the detector's own offset on the source
    assert len(err) >= 25
    assert abs(np.mean(err)) < 0.008
    assert np.std(err) < 0.006
    assert abs(np.mean(err[-8:]) - np.mean(err[:8])) < 0.006       # no drift


def test_pitch_glide_reaches_native():
    n = int(6 * SR)
    y = (0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / SR)).astype(np.float32)
    m = TimeMap([0.0, 6.0], [0.0, 6.0])
    out = warp(y[:, None], SR, m, lambda t: 5.0 * max(0.0, 1.0 - t / 3.0), formant=False)[:, 0]

    def f0(seg):
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n=1 << 18))
        return np.argmax(spec) * SR / (1 << 18)

    assert f0(out[int(0.2 * SR):int(0.6 * SR)]) == pytest.approx(220 * 2 ** (4.6 / 12), rel=0.03)
    assert f0(out[int(4.5 * SR):int(5.5 * SR)]) == pytest.approx(220, rel=0.01)
