"""Downward-inflection check (DESIGN §15): every sentence's last ~0.35 s of voiced speech must fall.

1. **Split** the spoken cue at its sentence ends: frame energy (20 ms frames, 10 ms hop) finds the
   pauses (runs below ``peak - SILENCE_DB``, at least ``MIN_GAP`` long); the ``n - 1`` longest
   interior pauses of an ``n``-sentence line are its sentence breaks (a full stop pauses longer
   than a comma). When the voice runs sentences together, fewer ends are found and measured.
2. **Track** F0 with pYIN (librosa) over the last ``WINDOW`` seconds before each sentence end.
3. **Fit** the voiced frames within ``TAIL`` seconds of the last modal voiced frame (plus any
   creak after it, which sits on pYIN's floor and counts as low pitch): the Theil–Sen slope of
   pitch in semitones per second (robust to a stray octave error). ``slope < 0`` falls.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

TAIL = 0.35        # s of voiced speech at the end of a sentence that must fall
WINDOW = 0.9       # s analyzed before a sentence end
MIN_GAP = 0.12     # s: shortest pause that may separate sentences
SILENCE_DB = 38.0  # dB below the loudest frame counts as a pause
MIN_FRAMES = 4     # voiced pYIN frames needed for a slope
FMIN, FMAX = 45.0, 320.0  # Hz: a baritone narrator, down into the creak of a final fall
CREAK_HZ = FMIN * 1.12    # pYIN estimates at its floor: creak, not modal voicing


@dataclass
class SentenceEnd:
    end: float               # s: where the sentence's sound stops
    slope: float | None      # semitones per second over the tail (None: not measurable)
    frames: int              # voiced frames in the tail
    start_hz: float | None
    end_hz: float | None

    @property
    def falls(self) -> bool:
        return self.slope is not None and self.slope < 0

    def as_dict(self) -> dict:
        return {"end": round(self.end, 3), "slope_st_per_s": None if self.slope is None else round(self.slope, 2),
                "frames": self.frames, "start_hz": None if self.start_hz is None else round(self.start_hz, 1),
                "end_hz": None if self.end_hz is None else round(self.end_hz, 1), "falls": self.falls}


def _frame_db(y: np.ndarray, sr: int, frame: float = 0.02, hop: float = 0.01) -> tuple[np.ndarray, float]:
    n, h = int(frame * sr), int(hop * sr)
    if len(y) < n:
        y = np.pad(y, (0, n - len(y)))
    idx = np.arange(0, len(y) - n + 1, h)
    cs = np.concatenate([[0.0], np.cumsum(y.astype(np.float64) ** 2)])
    rms = np.sqrt(np.maximum((cs[idx + n] - cs[idx]) / n, 1e-20))
    return 20 * np.log10(rms), hop


def pauses(y: np.ndarray, sr: int, min_gap: float = MIN_GAP, silence_db: float = SILENCE_DB) -> list[tuple[float, float]]:
    """Interior pauses [(start, end) seconds] — runs of quiet frames between sounds."""
    db, hop = _frame_db(y, sr)
    quiet = db < db.max() - silence_db
    loud = np.flatnonzero(~quiet)
    if not len(loud):
        return []
    first, last = loud[0], loud[-1]
    out, i = [], first
    while i <= last:
        if quiet[i]:
            j = i
            while j <= last and quiet[j]:
                j += 1
            if (j - i) * hop >= min_gap:
                out.append((i * hop + 0.01, j * hop))   # frame centres: sound stops ~ at i*hop+frame/2
            i = j
        else:
            i += 1
    return out


def sentence_ends(y: np.ndarray, sr: int, n_sentences: int) -> list[float]:
    """Times (s) where each sentence's sound stops: the ``n - 1`` longest pauses, then the end."""
    db, hop = _frame_db(y, sr)
    loud = np.flatnonzero(db >= db.max() - SILENCE_DB)
    if not len(loud):
        return []
    final = float(loud[-1] * hop + 0.02)
    gaps = sorted(pauses(y, sr), key=lambda g: g[1] - g[0], reverse=True)[: max(0, n_sentences - 1)]
    return sorted(g[0] for g in gaps) + [final]


def measure_end(y: np.ndarray, sr: int, end: float, window: float = WINDOW, tail: float = TAIL) -> SentenceEnd:
    """pYIN over ``window`` s before ``end``; Theil–Sen slope of the last ``tail`` s of voicing."""
    import librosa
    from scipy.stats import theilslopes

    a = max(0, int((end - window) * sr))
    b = min(len(y), int((end + 0.03) * sr))
    seg = y[a:b].astype(np.float32)
    frame = int(2 ** np.ceil(np.log2(2.2 * sr / FMIN)))   # two periods of FMIN fit in a frame
    hop = max(1, int(round(0.005 * sr)))
    if len(seg) < frame:
        return SentenceEnd(end, None, 0, None, None)
    f0, voiced, prob = librosa.pyin(seg, fmin=FMIN, fmax=FMAX, sr=sr, frame_length=frame, hop_length=hop,
                                    center=True)
    t = a / sr + np.arange(len(f0)) * hop / sr
    # pYIN's HMM voicing decision; its per-frame probability is low on breathy, falling endings
    ok = voiced & np.isfinite(f0)
    if not ok.any():
        return SentenceEnd(end, None, 0, None, None)
    # Creak (vocal fry) at the very end of a falling sentence sits on pYIN's floor; it counts as
    # low pitch, and the tail is anchored on the last modal (non-creak) voicing before it.
    modal = ok & (f0 >= CREAK_HZ)
    last = t[modal][-1] if modal.any() else t[ok][-1]
    sel = ok & (t >= last - tail)
    n = int(sel.sum())
    if n < MIN_FRAMES:
        return SentenceEnd(end, None, n, None, None)
    st = 12 * np.log2(f0[sel] / 440.0)
    slope = float(theilslopes(st, t[sel])[0])
    hz = f0[sel]
    k = max(1, n // 3)
    return SentenceEnd(end, slope, n, float(np.median(hz[:k])), float(np.median(hz[-k:])))


def check(y: np.ndarray, sr: int, n_sentences: int) -> dict:
    """Measure every sentence end. ``falls``: sentence ends whose pitch falls; ``of``: sentences in
    the line; ``measured``: ends found and tracked; ``ok``: no measured end rises and none is
    missing a measurement."""
    ends = [measure_end(y, sr, e) for e in sentence_ends(y, sr, n_sentences)]
    falls = sum(1 for e in ends if e.falls)
    measured = sum(1 for e in ends if e.slope is not None)
    return {"falls": falls, "of": int(n_sentences), "measured": measured,
            "ok": falls == n_sentences, "ends": [e.as_dict() for e in ends]}
