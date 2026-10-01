"""Audio for the narration cues: 48 kHz mono, trimmed, loudness-normalized to -16 LUFS
(ITU-R BS.1770-4 integrated loudness, true peak held under -1 dBTP), and the music duck that
puts the voice's speech band 10 dB above the mix's speech band under the cue (DESIGN §15).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

OUT_RATE = 48000
TARGET_LUFS = -16.0
MAX_TRUE_PEAK = -1.0           # dBTP
SPEECH_BAND = (300.0, 4000.0)  # Hz: where the music masks a voice
SPEECH_OVER_MUSIC_DB = 10.0
DUCK_RANGE = (-18.0, -6.0)     # dB of music gain under a cue
LEAD_IN = 0.04                 # s kept before the first sound
TAIL = 0.15                    # s kept after the last sound
EDGE_FADE = 0.008


# --------------------------------------------------------------------------- basic shaping
def resample(y: np.ndarray, sr: int, out: int = OUT_RATE) -> np.ndarray:
    if sr == out:
        return y.astype(np.float32)
    from scipy.signal import resample_poly

    g = math.gcd(int(sr), int(out))
    return resample_poly(y.astype(np.float64), out // g, sr // g).astype(np.float32)


def trim(y: np.ndarray, sr: int, threshold_db: float = -50.0) -> np.ndarray:
    """Cut leading/trailing silence (relative to the peak), keeping LEAD_IN / TAIL around the
    speech, with short fades at both ends."""
    if not len(y):
        raise ValueError("speech is empty")
    peak = float(np.abs(y).max())
    if peak <= 1e-6:
        raise ValueError("speech is silent")
    loud = np.flatnonzero(np.abs(y) > peak * 10 ** (threshold_db / 20))
    a = max(0, int(loud[0] - LEAD_IN * sr))
    b = min(len(y), int(loud[-1] + TAIL * sr) + 1)
    out = y[a:b].astype(np.float32).copy()
    e = min(int(EDGE_FADE * sr), len(out) // 2)
    if e > 0:
        ramp = np.linspace(0.0, 1.0, e, dtype=np.float32)
        out[:e] *= ramp
        out[-e:] *= ramp[::-1]
    return out


# --------------------------------------------------------------------------- BS.1770 loudness
def _k_weighting(sr: int) -> np.ndarray:
    """The two K-weighting biquads (high shelf, then high pass) for any sample rate, designed as
    libebur128 does (bilinear transform; identical to BS.1770-4's coefficients at 48 kHz).
    Returned as second-order sections."""
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = math.tan(math.pi * f0 / sr)
    vh = 10 ** (G / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    shelf = [(vh + vb * K / Q + K * K) / a0, 2 * (K * K - vh) / a0, (vh - vb * K / Q + K * K) / a0,
             1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = math.tan(math.pi * f0 / sr)
    a0 = 1 + K / Q + K * K
    hp = [1.0, -2.0, 1.0, 1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    return np.array([shelf, hp])


def integrated_loudness(y: np.ndarray, sr: int) -> float:
    """BS.1770-4 integrated loudness (LUFS) of a mono signal: K-weighting, 400 ms blocks with
    75 % overlap, absolute gate -70 LUFS, relative gate -10 LU. -inf for silence."""
    from scipy.signal import sosfilt

    sos = _k_weighting(sr)
    z = sosfilt(sos, y.astype(np.float64))
    block, step = int(0.4 * sr), int(0.1 * sr)
    if len(z) < block:
        z = np.pad(z, (0, block - len(z)))
    sq = z * z
    cs = np.concatenate([[0.0], np.cumsum(sq)])
    starts = np.arange(0, len(z) - block + 1, step)
    ms = (cs[starts + block] - cs[starts]) / block
    with np.errstate(divide="ignore"):
        lk = -0.691 + 10 * np.log10(ms)
    gated = ms[lk > -70.0]
    if not len(gated):
        return float("-inf")
    rel = -0.691 + 10 * math.log10(gated.mean()) - 10.0
    gated = ms[(lk > -70.0) & (lk > rel)]
    return float(-0.691 + 10 * math.log10(gated.mean()))


def true_peak_db(y: np.ndarray, sr: int) -> float:
    """True peak (dBTP) estimated with 4x oversampling (BS.1770-4 annex 2)."""
    from scipy.signal import resample_poly

    over = 4 if sr < 96000 else 2
    p = float(np.abs(resample_poly(y.astype(np.float64), over, 1)).max()) if len(y) else 0.0
    return 20 * math.log10(max(p, 1e-12))


def _limit(y: np.ndarray, sr: int, ceiling_db: float) -> np.ndarray:
    """Look-ahead peak limiter: gain ramps down 5 ms before an over and recovers over 80 ms."""
    from scipy.ndimage import maximum_filter1d, uniform_filter1d
    from scipy.signal import resample_poly

    ceiling = 10 ** (ceiling_db / 20)
    up = np.abs(resample_poly(y.astype(np.float64), 4, 1))
    peak = up[: 4 * len(y)].reshape(-1, 4).max(axis=1) if len(up) >= 4 * len(y) else np.abs(y)
    need = np.minimum(1.0, ceiling / np.maximum(peak, 1e-12))
    a = max(1, int(0.005 * sr))
    g = -maximum_filter1d(-need, size=2 * a + 1, mode="nearest")
    g = uniform_filter1d(g, size=a, mode="nearest")
    k = math.exp(-1.0 / (0.08 * sr))
    red = 1.0 - g
    out = np.empty_like(red)
    h = 0.0
    for i, r in enumerate(red):          # decaying hold of the gain reduction (release)
        h = r if r > h * k else h * k
        out[i] = h
    gain = np.minimum(1.0 - out, need)
    return (y * gain).astype(np.float32)


def normalize(y: np.ndarray, sr: int, target: float = TARGET_LUFS, max_tp: float = MAX_TRUE_PEAK) -> tuple[np.ndarray, dict]:
    """Gain to ``target`` LUFS; where that pushes the true peak over ``max_tp`` a look-ahead limiter
    holds the peaks and the loudness the limiter took is made up again (a few passes)."""
    pre = integrated_loudness(y, sr)
    if not math.isfinite(pre):
        raise ValueError("speech has no measurable loudness")
    gain_db = target - pre
    src = y.astype(np.float32)
    out = (src * np.float32(10 ** (gain_db / 20))).astype(np.float32)
    limited = False
    for _ in range(4):
        if true_peak_db(out, sr) <= max_tp:
            break
        limited = True
        out = _limit(out, sr, max_tp - 0.3)
        miss = target - integrated_loudness(out, sr)
        if abs(miss) < 0.15:
            break
        gain_db += miss
        out = (src * np.float32(10 ** (gain_db / 20))).astype(np.float32)
    else:
        if true_peak_db(out, sr) > max_tp:
            out = _limit(out, sr, max_tp - 0.3)
    return out, {"lufs_in": round(pre, 2), "gain_db": round(gain_db, 2), "limited": bool(limited),
                 "lufs": round(integrated_loudness(out, sr), 2), "true_peak": round(true_peak_db(out, sr), 2)}


# --------------------------------------------------------------------------- duck
def band_level(y: np.ndarray, sr: int, start: float = 0.0, end: float | None = None) -> float:
    """Speech-band (300 Hz - 4 kHz) level in dBFS between ``start`` and ``end`` seconds: the 90th
    percentile RMS of 400 ms frames (hop 200 ms), so a loud moment inside the line counts.
    -inf when the stretch is (nearly) empty or silent."""
    from scipy.signal import butter, sosfiltfilt

    a = max(0, int(start * sr))
    b = len(y) if end is None else min(len(y), int(end * sr))
    if b - a < int(0.1 * sr):
        return float("-inf")
    hi = min(SPEECH_BAND[1], 0.45 * sr)
    seg = sosfiltfilt(butter(4, [SPEECH_BAND[0], hi], btype="band", fs=sr, output="sos"), y[a:b].astype(np.float64))
    frame = int(0.4 * sr)
    if len(seg) < frame:
        frames = [seg]
    else:
        frames = [seg[i: i + frame] for i in range(0, len(seg) - frame + 1, frame // 2)]
    rms = float(np.percentile([math.sqrt(float(np.mean(f * f))) for f in frames], 90))
    return 20 * math.log10(rms) if rms > 1e-9 else float("-inf")


def duck_db(speech_db: float, music_db: float, over: float = SPEECH_OVER_MUSIC_DB,
            lo: float = DUCK_RANGE[0], hi: float = DUCK_RANGE[1]) -> float:
    """Music gain (dB) that puts the voice's speech band ``over`` dB above the music's speech band
    (both played at unity), clamped to [lo, hi]: a quiet passage still dips by ``hi``."""
    if not math.isfinite(music_db):
        return hi
    if not math.isfinite(speech_db):
        return lo
    return round(float(min(hi, max(lo, speech_db - over - music_db))), 1)


def read_mono(path: Path) -> tuple[np.ndarray, int]:
    import soundfile

    y, sr = soundfile.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1), int(sr)


def write_wav(path: Path, y: np.ndarray, sr: int = OUT_RATE) -> None:
    import os

    import soundfile

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.wav")
    soundfile.write(str(tmp), np.clip(y, -1.0, 1.0), sr, subtype="PCM_16")
    os.replace(tmp, path)
