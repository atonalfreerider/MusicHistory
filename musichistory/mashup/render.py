"""Render a chain timeline to a stereo 44.1 kHz mix.

Every (song, stem) that sounds somewhere is rendered on the mix timeline from the song's
pieces (``timeline.Piece``): native pieces are copied sample for sample, warped pieces go
through ``warp.warp`` (beat time map + transposition curve; formants preserved for the vocal).
Pieces of one song meet at bar lines with a 20 ms crossfade (linear where the same source
continues, equal-power at a loop jump). Each stem is then gated by its envelope: equal-power
fades of half a beat (at most ``MAX_FADE``) centred on the bar line where it enters or leaves.

Levels: every song is scaled so its preview's RMS level is ``REF_DB`` (songs are mastered
differently), a changeover lead gets ``LEAD_BOOST_DB`` over the backing (ramping back to 0 dB
through its morph), and the finished mix is normalized to ``TARGET_LUFS`` integrated loudness
(ffmpeg ``loudnorm`` measurement, linear gain); where that gain would push the true peak over
``MAX_TRUE_PEAK`` dBTP a look-ahead peak limiter (``limit``) holds the peaks under it; 20 ms fade-in, the last ``FADE_OUT_BARS`` bars fade out. MP3:
libmp3lame VBR q2.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import numpy as np

from .timeline import Timeline
from .warp import TimeMap, warp

SR = 44100
REF_DB = -18.0
LEAD_BOOST_DB = 1.5
TARGET_LUFS = -16.0
MAX_TRUE_PEAK = -1.0
TP_MARGIN = 0.5              # MP3 encoding overshoot allowance
MARGIN = 0.3                 # source seconds rendered around a piece (fades, crossfades)
JOIN = 0.010                 # half-width of the crossfade where two pieces meet
MAX_FADE = 0.3
FADE_IN = 0.02
FADE_OUT_BARS = 2
LEAD_STEMS = ("vocals",)
LIMIT_HEADROOM = 0.5         # the limiter's ceiling sits this far under the true-peak target


# --------------------------------------------------------------------------- helpers
def crop_map(src: np.ndarray, dst: np.ndarray, lo: float, hi: float) -> TimeMap:
    """The map restricted to output times [lo, hi] (endpoints interpolated)."""
    lo, hi = max(lo, float(dst[0])), min(hi, float(dst[-1]))
    inner = (dst > lo) & (dst < hi)
    d = np.r_[lo, dst[inner], hi]
    s = np.r_[np.interp(lo, dst, src), src[inner], np.interp(hi, dst, src)]
    return TimeMap(s, d)


def extend(src: np.ndarray, dst: np.ndarray, margin: float, src_end: float) -> tuple[np.ndarray, np.ndarray]:
    """Extend a piece's points by ``margin`` source seconds at both ends (first/last slope)."""
    k0 = (dst[1] - dst[0]) / (src[1] - src[0])
    k1 = (dst[-1] - dst[-2]) / (src[-1] - src[-2])
    m0 = min(margin, float(src[0]))
    m1 = min(margin, src_end - float(src[-1]))
    if m0 > 1e-3:
        src, dst = np.r_[src[0] - m0, src], np.r_[dst[0] - m0 * k0, dst]
    if m1 > 1e-3:
        src, dst = np.r_[src, src[-1] + m1], np.r_[dst, dst[-1] + m1 * k1]
    return src, dst


def ramp(n: int, kind: str) -> np.ndarray:
    u = (np.arange(n) + 0.5) / max(n, 1)
    return np.sin(0.5 * np.pi * u) if kind == "power" else u


def fade_len(tl: Timeline, t: float) -> float:
    i = int(np.clip(np.searchsorted(tl.grid, t), 1, len(tl.grid) - 1))
    beat = float(tl.grid[i] - tl.grid[i - 1])
    return min(0.5 * beat, MAX_FADE)


def envelope(tl: Timeline, intervals, n: int, sr: int = SR) -> np.ndarray:
    """Gain curve of one stem: 1 inside its intervals, equal-power fades centred on the
    boundaries (none at the very start or end of the mix)."""
    g = np.zeros(n, dtype=np.float32)
    end = tl.seconds
    for t0, t1 in intervals:
        f0 = 0.0 if t0 <= 1e-6 else fade_len(tl, t0)
        f1 = 0.0 if t1 >= end - 1e-6 else fade_len(tl, t1)
        a, b = int(round((t0 - f0 / 2) * sr)), int(round((t0 + f0 / 2) * sr))
        c, d = int(round((t1 - f1 / 2) * sr)), int(round((t1 + f1 / 2) * sr))
        a, d = max(a, 0), min(d, n)
        g[max(b, 0):min(c, n)] = 1.0
        if b > a:
            g[a:b] = np.maximum(g[a:b], ramp(b - a, "power")[: b - a][-(b - a):].astype(np.float32))
        if d > c:
            g[c:d] = np.maximum(g[c:d], ramp(d - c, "power")[::-1].astype(np.float32))
    return g


# --------------------------------------------------------------------------- tracks
def render_stem(tl: Timeline, song: int, stem: str, audio: np.ndarray, sr: int = SR,
                intervals=None) -> tuple[np.ndarray, list[float]]:
    """One stem of one song on the mix timeline (before its envelope). Returns (audio,
    the mix times where two of its pieces meet)."""
    n = int(np.ceil(tl.seconds * sr)) + 1
    out = np.zeros((n, audio.shape[1]), dtype=np.float32)
    src_end = len(audio) / sr
    joins: list[float] = []
    ivs = intervals if intervals is not None else tl.envelopes.get((song, stem), [])
    formant = stem in LEAD_STEMS
    for p in tl.song_pieces(song):
        s_ext, d_ext = extend(p.src_times, p.out_times, MARGIN, src_end)
        lo = max(float(d_ext[0]), 0.0)
        hi = min(float(d_ext[-1]), tl.seconds)
        need = [(a, b) for a, b in ivs if a < hi + MARGIN and b > lo - MARGIN]
        if not need or hi - lo < 1e-3:
            continue
        lo = max(lo, min(a for a, _ in need) - MARGIN)
        hi = min(hi, max(b for _, b in need) + MARGIN)
        if hi - lo < 1e-3:
            continue
        m = crop_map(s_ext, d_ext, lo, hi)
        i0 = int(round(m.dst[0] * sr))
        if p.warped:
            s0 = int(round(m.src[0] * sr))
            seg = audio[s0:int(np.ceil(m.src[-1] * sr)) + 1]
            y = warp(seg, sr, m, (lambda t, d0=float(m.dst[0]): tl.semitones(song, d0 + t)), formant=formant)
        else:
            s0 = int(round(m.src[0] * sr))
            y = audio[s0:s0 + int(round((m.dst[-1] - m.dst[0]) * sr))]
        w = np.ones(len(y), dtype=np.float32)
        j = int(round(JOIN * sr))
        if p.join_prev:
            c = int(round(p.out_start * sr)) - i0
            r = ramp(2 * j, "power" if p.join_prev == "jump" else "linear").astype(np.float32)
            a, b = max(c - j, 0), max(min(c + j, len(w)), 0)
            w[:a] = 0.0
            if b > a:
                w[a:b] = r[(a - (c - j)):(b - (c - j))]
            joins.append(p.out_start)
        if p.join_next:
            c = int(round(p.out_end * sr)) - i0
            r = ramp(2 * j, "power" if p.join_next == "jump" else "linear")[::-1].astype(np.float32)
            a, b = max(min(c - j, len(w)), 0), max(min(c + j, len(w)), 0)
            w[b:] = 0.0
            if b > a:
                w[a:b] = np.minimum(w[a:b], r[(a - (c - j)):(b - (c - j))])
        i1 = min(n, i0 + len(y))
        if i1 > i0 and i0 >= 0:
            out[i0:i1] += y[: i1 - i0] * w[: i1 - i0, None]
    return out, joins


class StemCache:
    """Stems of the path's songs at ``SR`` (stereo float32), loaded on demand."""

    def __init__(self, dirs: dict[int, Path]):
        self.dirs = dirs
        self.cache: dict[tuple[int, str], np.ndarray] = {}

    def get(self, song: int, stem: str) -> np.ndarray:
        key = (song, stem)
        if key not in self.cache:
            import soundfile

            y, sr = soundfile.read(str(self.dirs[song] / f"{stem}.wav"), dtype="float32", always_2d=True)
            if sr != SR:
                import librosa

                y = librosa.resample(y.T, orig_sr=sr, target_sr=SR).T.astype(np.float32)
            if y.shape[1] == 1:
                y = np.repeat(y, 2, axis=1)
            self.cache[key] = y[:, :2]
        return self.cache[key]


def song_gain(levels: dict) -> float:
    mix = float(levels.get("mix", REF_DB)) if levels else REF_DB
    return float(10 ** ((REF_DB - mix) / 20))


def lead_gain_curve(tl: Timeline, song: int, n: int, sr: int = SR) -> np.ndarray:
    """LEAD_BOOST_DB through the song's changeover lead, ramping to 0 dB over its morph."""
    g = np.ones(n, dtype=np.float32)
    if song not in tl.shifts:
        return g
    boost = 10 ** (LEAD_BOOST_DB / 20)
    m = tl.morphs.get(song)
    t = np.arange(n) / sr
    if m is None:
        return np.full(n, boost, dtype=np.float32)
    g[t < m[0]] = boost
    sel = (t >= m[0]) & (t < m[1])
    g[sel] = boost + (1 - boost) * (t[sel] - m[0]) / (m[1] - m[0])
    return g


def render_buses(tl: Timeline, stems: StemCache, levels: dict[int, dict], sr: int = SR,
                 log=None) -> tuple[np.ndarray, np.ndarray, list[float]]:
    """(lead bus, backing bus, piece-join times) of the whole mix."""
    n = int(np.ceil(tl.seconds * sr)) + 1
    lead = np.zeros((n, 2), dtype=np.float32)
    back = np.zeros((n, 2), dtype=np.float32)
    joins: list[float] = []
    tracks = tl.plan.tracks
    for (song, stem), ivs in sorted(tl.envelopes.items()):
        audio = stems.get(song, stem)
        y, jn = render_stem(tl, song, stem, audio, sr, ivs)
        env = envelope(tl, ivs, n, sr)
        g = song_gain(levels.get(song, {}))
        is_lead = stem == "vocals" or (stem == tracks[song].lead and stem != "instruments")
        if is_lead:
            env = env * lead_gain_curve(tl, song, n, sr)
        (lead if is_lead else back)[:] += y * (env * g)[:, None]
        joins += jn
        if log:
            log(f"    rendered {tracks[song].work_id} {stem}")
    return lead, back, sorted(set(round(j, 4) for j in joins))


def master(lead: np.ndarray, back: np.ndarray, tl: Timeline, sr: int = SR) -> np.ndarray:
    mix = (lead + back).astype(np.float32)
    n = len(mix)
    k = int(FADE_IN * sr)
    mix[:k] *= np.linspace(0, 1, k, dtype=np.float32)[:, None]
    bars = len(tl.plan.bars)
    t0 = tl.bar_span(max(0, bars - FADE_OUT_BARS))[0]
    a = int(t0 * sr)
    if a < n:
        u = np.linspace(0, 1, n - a, dtype=np.float32)
        mix[a:] *= (np.cos(0.5 * np.pi * u) ** 2)[:, None]
    return mix


# --------------------------------------------------------------------------- loudness / encode
def _ffmpeg(ffmpeg: str, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", *args], cwd=str(cwd), capture_output=True, text=True,
                       errors="replace")
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed ({r.returncode}): {r.stderr.strip()[-800:]}")
    return r


def loudness(ffmpeg: str, path: Path) -> dict:
    """Integrated loudness (LUFS) and true peak (dBTP) of an audio file (ffmpeg loudnorm)."""
    r = _ffmpeg(ffmpeg, ["-i", str(path), "-af", "loudnorm=I=-16:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                path.parent)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        raise RuntimeError("loudnorm printed no measurement")
    doc = json.loads(m.group(0))
    return {"lufs": float(doc["input_i"]), "true_peak": float(doc["input_tp"]), "lra": float(doc["input_lra"])}


def _decay_hold_py(x: np.ndarray, k: float) -> np.ndarray:
    """h[i] = max(x[i], h[i-1] * k): a peak hold that decays exponentially."""
    h = x.astype(np.float64)
    for i in range(1, len(h)):
        if h[i - 1] * k > h[i]:
            h[i] = h[i - 1] * k
    return h


try:
    from numba import njit

    _decay_hold = njit(cache=False)(_decay_hold_py)
except ImportError:  # pragma: no cover
    _decay_hold = _decay_hold_py


def limit(y: np.ndarray, sr: int, ceiling: float, attack: float = 0.005, release: float = 0.08) -> np.ndarray:
    """Look-ahead peak limiter (linked stereo): the gain reaching each peak over ``ceiling``
    ramps down over ``attack`` before it (so the peak passes at the ceiling) and recovers
    exponentially over ``release``. 4x-oversampled peaks stand in for true peaks."""
    from scipy.ndimage import maximum_filter1d, uniform_filter1d
    from scipy.signal import resample_poly

    up = np.abs(resample_poly(y, 4, 1, axis=0)).max(axis=1)
    peak = up.reshape(-1, 4).max(axis=1)[: len(y)] if len(up) >= 4 * len(y) else np.abs(y).max(axis=1)
    need = np.minimum(1.0, ceiling / np.maximum(peak, 1e-9))
    a = max(1, int(attack * sr))
    g = -maximum_filter1d(-need, size=2 * a + 1, mode="nearest")      # the minimum gain within +-attack
    g = uniform_filter1d(g, size=a, mode="nearest")
    out = 1.0 - _decay_hold(np.ascontiguousarray(1.0 - g, dtype=np.float64), float(np.exp(-1.0 / (release * sr))))    # release: recover slowly
    out = np.minimum(out, need)                                       # never above what a peak allows
    return (y * out[:, None]).astype(np.float32)


def write_mp3(ffmpeg: str, mix: np.ndarray, dst: Path, sr: int = SR) -> dict:
    """Normalize ``mix`` to TARGET_LUFS (true peak <= MAX_TRUE_PEAK - TP_MARGIN) and encode."""
    import soundfile

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f"_{dst.stem}.tmp.wav"
    part = dst.parent / f"_{dst.stem}.part.mp3"
    try:
        soundfile.write(str(tmp), mix, sr, subtype="FLOAT")
        pre = loudness(ffmpeg, tmp)
        gain_db = TARGET_LUFS - pre["lufs"]
        limited = pre["true_peak"] + gain_db > MAX_TRUE_PEAK - TP_MARGIN
        y = (mix * np.float32(10 ** (gain_db / 20))).astype(np.float32)
        if limited:
            y = limit(y, sr, 10 ** ((MAX_TRUE_PEAK - TP_MARGIN - LIMIT_HEADROOM) / 20))
        soundfile.write(str(tmp), y, sr, subtype="FLOAT")
        _ffmpeg(ffmpeg, ["-y", "-v", "error", "-i", tmp.name, "-ar", str(sr), "-ac", "2", "-c:a", "libmp3lame",
                         "-q:a", "2", part.name], dst.parent)
        os.replace(part, dst)
        post = loudness(ffmpeg, dst)
        return {"gain_db": round(gain_db, 2), "limited": bool(limited), "pre": pre, "lufs": post["lufs"],
                "true_peak": post["true_peak"], "normalized": y}
    finally:
        tmp.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
