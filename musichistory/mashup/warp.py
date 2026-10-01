"""Beat-synchronous time-map stretching with Rubber Band (``pylibrb``, Rubber Band 3 / R3).

``warp(audio, sr, src_points, dst_points, semitones)`` plays ``audio`` so that every source
time in ``src_points`` lands on the matching output time in ``dst_points`` (piecewise linear
in between), transposed by ``semitones(output seconds)`` with formants preserved.

pylibrb 0.1.2's ``set_keyframe_map`` cannot be called (its binding has no converter for the
std::map argument), so the map is followed in Rubber Band's real-time mode instead: the input
is fed in blocks of ``BLOCK`` samples and before each block the time ratio is set so that the
output owed so far equals the map at the block's end (``ratio = (map(end) - committed) /
block``; the error never accumulates). Rubber Band's start latency is ``get_start_delay()``
input samples, i.e. ``delay * ratio`` output samples, which are dropped. Measured on click
tracks (tests/mashup/test_warp.py): onsets land within a few ms of their mapped times, with no
drift.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

BLOCK = 512


class TimeMap:
    """Monotonic piecewise-linear map from source seconds to output seconds."""

    def __init__(self, src: np.ndarray, dst: np.ndarray):
        src = np.asarray(src, dtype=float)
        dst = np.asarray(dst, dtype=float)
        if len(src) != len(dst) or len(src) < 2:
            raise ValueError("a time map needs at least two matching points")
        if np.any(np.diff(src) <= 0) or np.any(np.diff(dst) <= 0):
            raise ValueError("time map points must increase strictly")
        self.src, self.dst = src, dst

    def __call__(self, t):
        return np.interp(t, self.src, self.dst)

    def inverse(self, t):
        return np.interp(t, self.dst, self.src)

    def slopes(self) -> np.ndarray:
        """Output seconds per source second on each interval (> 1: slower)."""
        return np.diff(self.dst) / np.diff(self.src)


def beat_map(src_beats, dst_beats, margin: float = 0.0, src_limit: tuple[float, float] | None = None) -> TimeMap:
    """Map beat i of the source onto beat i of the output, extended by ``margin`` source
    seconds at both ends with the slope of the first/last beat (clipped to ``src_limit``)."""
    s = np.asarray(src_beats, dtype=float)
    d = np.asarray(dst_beats, dtype=float)
    if margin > 0:
        lo, hi = src_limit if src_limit else (-np.inf, np.inf)
        k0 = (d[1] - d[0]) / (s[1] - s[0])
        k1 = (d[-1] - d[-2]) / (s[-1] - s[-2])
        m0 = min(margin, s[0] - lo)
        m1 = min(margin, hi - s[-1])
        if m0 > 1e-4:
            s, d = np.r_[s[0] - m0, s], np.r_[d[0] - m0 * k0, d]
        if m1 > 1e-4:
            s, d = np.r_[s, s[-1] + m1], np.r_[d, d[-1] + m1 * k1]
    return TimeMap(s, d)


_LATENCY: dict = {}


def _stretch_const(x: np.ndarray, sr: int, opts: int, ratio: float, semis: float, block: int) -> np.ndarray:
    """Constant-ratio real-time stretch of a mono signal, nothing dropped."""
    import pylibrb

    st = pylibrb.RubberBandStretcher(sr, 1, opts, ratio, 2.0 ** (semis / 12.0))
    pad = int(st.get_preferred_start_pad())
    if pad:
        st.process(np.zeros((1, pad), dtype=np.float32), False)
    chunks = []
    for pos in range(0, len(x), block):
        st.time_ratio = ratio
        st.process(np.ascontiguousarray(x[None, pos:pos + block]), pos + block >= len(x))
        a = st.available()
        if a > 0:
            chunks.append(st.retrieve(a)[0])
    while (a := st.available()) > 0:
        chunks.append(st.retrieve(a)[0])
    return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)


def latency(sr: int, ch: int, opts: int, ratio: float, semis: float, block: int = BLOCK) -> int:
    """Output samples to drop: measured once per (ratio, transposition) by stretching a click
    train at that constant ratio and locating the clicks (Rubber Band's own
    ``get_start_delay`` is only exact at ratio 1)."""
    key = (sr, int(opts), round(ratio, 3), round(semis, 2), block)
    if key in _LATENCY:
        return _LATENCY[key]
    from scipy.signal import butter, sosfiltfilt

    period, n_clicks = 0.25, 12
    n = int((period * (n_clicks + 2)) * sr)
    x = np.zeros(n, np.float32)
    burst = (np.hanning(64) * np.sin(2 * np.pi * 2000 * np.arange(64) / sr)).astype(np.float32)
    starts = [int((k + 1) * period * sr) for k in range(n_clicks)]
    for i in starts:
        x[i:i + 64] += burst
    y = _stretch_const(x, sr, opts, ratio, semis, block)
    env = np.abs(sosfiltfilt(butter(2, 500, "hp", fs=sr, output="sos"), y))
    ref = np.abs(sosfiltfilt(butter(2, 500, "hp", fs=sr, output="sos"), x))
    offs = []
    half = int(period * ratio * sr / 2)
    for i in starts:
        def onset(sig, centre):
            w = sig[max(0, centre - half):centre + half]
            return max(0, centre - half) + int(np.argmax(w > 0.3 * w.max())) if len(w) and w.max() > 0 else None
        r = onset(ref, i + 32)
        if r is None:
            continue
        guess = int(round(r * ratio)) + int(round(2048 * ratio))
        o = onset(env, guess)
        if o is not None:
            offs.append(o - r * ratio)
    lat = int(round(float(np.median(offs)))) if offs else 0
    _LATENCY[key] = lat
    return lat


def warp(audio: np.ndarray, sr: int, tmap: TimeMap, semitones: Callable[[float], float] | float = 0.0, *,
         formant: bool = True, block: int = BLOCK) -> np.ndarray:
    """Render ``audio`` ((n, ch) float32, time 0 = ``tmap.src[0]``) through the map. Returns
    (round((dst[-1] - dst[0]) * sr), ch) float32; output time 0 = ``tmap.dst[0]``."""
    import pylibrb

    a = np.asarray(audio, dtype=np.float32)
    if a.ndim == 1:
        a = a[:, None]
    ch = a.shape[1]
    s0, d0 = float(tmap.src[0]), float(tmap.dst[0])
    n_in = min(len(a), int(round((tmap.src[-1] - s0) * sr)))
    n_out = int(round((tmap.dst[-1] - d0) * sr))
    pitch = semitones if callable(semitones) else (lambda t, v=float(semitones): v)

    def out_at(i: float) -> float:        # output samples owed after ``i`` input samples
        return (float(tmap(s0 + i / sr)) - d0) * sr

    O = pylibrb.Option
    opts = (O.PROCESS_REALTIME | O.ENGINE_FINER | O.PitchHighConsistency | O.CHANNELS_TOGETHER
            | (O.FORMANT_PRESERVED if formant else O.FORMANT_SHIFTED))
    r0 = max(0.05, out_at(min(block, n_in)) / max(1, min(block, n_in)))
    st = pylibrb.RubberBandStretcher(sr, ch, opts, r0, 2.0 ** (pitch(0.0) / 12.0))
    pad = int(st.get_preferred_start_pad())
    drop = latency(sr, ch, opts, r0, pitch(0.0), block)
    if pad:
        st.process(np.zeros((ch, pad), dtype=np.float32), False)
    chunks: list[np.ndarray] = []
    got = 0
    committed = 0.0
    pos = 0
    x = np.ascontiguousarray(a[:n_in].T)
    while pos < n_in:
        b = min(block, n_in - pos)
        ratio = max(0.05, (out_at(pos + b) - committed) / b)
        st.time_ratio = ratio
        st.pitch_scale = 2.0 ** (pitch(max(0.0, committed / sr)) / 12.0)
        committed += b * ratio
        st.process(np.ascontiguousarray(x[:, pos:pos + b]), pos + b >= n_in)
        pos += b
        avail = st.available()
        if avail > 0:
            c = st.retrieve(avail)
            chunks.append(c)
            got += c.shape[1]
    while True:
        avail = st.available()
        if avail <= 0:
            break
        c = st.retrieve(avail)
        chunks.append(c)
        got += c.shape[1]
    y = np.concatenate(chunks, axis=1).T if chunks else np.zeros((0, ch), dtype=np.float32)
    y = y[drop:drop + n_out]
    if len(y) < n_out:
        y = np.vstack([y, np.zeros((n_out - len(y), ch), dtype=np.float32)])
    return y.astype(np.float32)
