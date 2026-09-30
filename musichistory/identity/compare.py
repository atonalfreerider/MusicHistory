"""Agreement between two identities, and with Hooktheory annotations (0..1).

Used by select to find the consensus transcription of a song (DESIGN.md §6). Both scores
are local alignments (Smith-Waterman-Gotoh) normalized by self-alignment, so identical
material scores 1 and missing or contradicting material lowers the score:

    agreement(a, b) = SW(a, b) / sqrt(SW(a, a) * SW(b, b))

* **chords**: L1 ``chg`` tokens, ``s(x, y) = 3 J(x, y) - 1`` (+0.25 same root, different
  quality), J = Jaccard of the triads' pitch-class sets; gaps open 2.5, extend 0.75
  (mir-methods.md §2.5).
* **melody**: the lead line with repeated same-pitch notes merged (syllable splitting is
  the commonest difference between transcriptions); +2 same pitch class, 0 at 1-2
  semitones, -1 otherwise, times 1.0 for the same metric class and 0.75 otherwise; gaps
  open 3, extend 0.3 (§3.4).

When either song's key is flagged ``key_ambiguous_fifth``, the other song is also tried a
fifth up and down (3-point penalty), as influence does.

Sequences are short (tens to hundreds of tokens), so a row-vectorized numpy Gotoh with the
"lazy F" prefix-max trick is fast enough in Python (~10-40 ms per melody pair).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .chords import l1
from .key import MAJOR, MINOR, shift_for

CHORD_GAP = (2.5, 0.75)
MELODY_GAP = (3.0, 0.3)
FIFTH_PENALTY = 3.0
MIN_CHORDS = 4
MIN_NOTES = 8
TRIADS = {0: (0, 4, 7), 1: (0, 3, 7), 2: (0, 3, 6)}


def _chord_matrix() -> np.ndarray:
    m = np.zeros((36, 36))
    sets = [frozenset((t // 3 + x) % 12 for x in TRIADS[t % 3]) for t in range(36)]
    for a in range(36):
        for b in range(36):
            j = len(sets[a] & sets[b]) / len(sets[a] | sets[b])
            m[a, b] = 3 * j - 1 + (0.25 if a != b and a // 3 == b // 3 else 0.0)
    return m


CHORD_SUB = _chord_matrix()


def smith_waterman(sub: np.ndarray, gap_open: float, gap_extend: float) -> float:
    """Best local alignment score for a substitution matrix ``sub[i, j]`` with affine gaps
    (a gap of length L costs open + extend * (L - 1))."""
    if sub.shape[0] > sub.shape[1]:
        sub = sub.T  # fewer Python-level rows; the score is symmetric in the two sequences
    n, m = sub.shape
    if n == 0 or m == 0:
        return 0.0
    h_prev = np.zeros(m + 1)
    e = np.full(m + 1, -np.inf)
    ramp = np.arange(m + 1) * gap_extend
    best = 0.0
    for i in range(n):
        e = np.maximum(e - gap_extend, h_prev - gap_open)            # vertical gaps
        hp = np.empty(m + 1)
        hp[0] = 0.0
        np.maximum(np.maximum(h_prev[:-1] + sub[i], e[1:]), 0.0, out=hp[1:])
        # Horizontal gaps from the prefix max of hp[k] + extend*k ("lazy F"; exact for open >= extend).
        acc = np.maximum.accumulate(hp + ramp)
        f = np.full(m + 1, -np.inf)
        f[1:] = acc[:-1] - gap_open - ramp[:-1]
        h = np.maximum(hp, f)
        h[0] = 0.0
        row_best = float(h.max())
        if row_best > best:
            best = row_best
        h_prev = h
    return best


# --------------------------------------------------------------------------- chords
def _transpose_l1(tokens: np.ndarray, semis: int) -> np.ndarray:
    return ((tokens // 3 + semis) % 12) * 3 + tokens % 3


def chord_score(x: list[int], y: list[int], *, fifth_hedge: bool = False) -> float | None:
    if len(x) < MIN_CHORDS or len(y) < MIN_CHORDS:
        return None
    a, b = np.asarray(x), np.asarray(y)
    raw = smith_waterman(CHORD_SUB[a[:, None], b[None, :]], *CHORD_GAP)
    if fifth_hedge:
        for s in (5, 7):
            bt = _transpose_l1(b, s)
            raw = max(raw, smith_waterman(CHORD_SUB[a[:, None], bt[None, :]], *CHORD_GAP) - FIFTH_PENALTY)
    return _normalize(raw, 2.0 * len(x), 2.0 * len(y))


def _normalize(raw: float, self_a: float, self_b: float) -> float:
    if self_a <= 0 or self_b <= 0:
        return 0.0
    return round(max(0.0, min(1.0, raw / math.sqrt(self_a * self_b))), 4)


def chord_agreement(a: Any, b: Any) -> float | None:
    """0..1 key-normalized L1 chord-change alignment (None when either has < 4 changes)."""
    hedge = bool(getattr(a, "key_ambiguous_fifth", False) or getattr(b, "key_ambiguous_fifth", False))
    return chord_score(a.tokens("chg", "L1"), b.tokens("chg", "L1"), fifth_hedge=hedge)


# --------------------------------------------------------------------------- melody
def melody_tokens(pitches: list[int], met: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """Pitch classes with repeated same-pitch notes merged (keeps the first note's metric class)."""
    pcs, mets = [], []
    last = None
    for p, c in zip(pitches, met):
        if p == last:
            continue
        pcs.append(p % 12)
        mets.append(c)
        last = p
    return np.asarray(pcs, dtype=int), np.asarray(mets, dtype=int)


def melody_sub(pa: np.ndarray, ma: np.ndarray, pb: np.ndarray, mb: np.ndarray) -> np.ndarray:
    d = np.abs(pa[:, None] - pb[None, :]) % 12
    d = np.minimum(d, 12 - d)
    base = np.where(d == 0, 2.0, np.where(d <= 2, 0.0, -1.0))
    return base * np.where(ma[:, None] == mb[None, :], 1.0, 0.75)


def melody_score(pa: np.ndarray, ma: np.ndarray, pb: np.ndarray, mb: np.ndarray, *,
                 fifth_hedge: bool = False) -> float | None:
    if len(pa) < MIN_NOTES or len(pb) < MIN_NOTES:
        return None
    raw = smith_waterman(melody_sub(pa, ma, pb, mb), *MELODY_GAP)
    if fifth_hedge:
        for s in (5, 7):
            raw = max(raw, smith_waterman(melody_sub(pa, ma, (pb + s) % 12, mb), *MELODY_GAP) - FIFTH_PENALTY)
    return _normalize(raw, 2.0 * len(pa), 2.0 * len(pb))


def melody_agreement(a: Any, b: Any) -> float | None:
    """0..1 normalized lead-line alignment (None when either has no usable melody)."""
    if a.melody is None or b.melody is None:
        return None
    pa, ma = melody_tokens(a.melody.pitches, a.melody.met)
    pb, mb = melody_tokens(b.melody.pitches, b.melody.met)
    hedge = bool(getattr(a, "key_ambiguous_fifth", False) or getattr(b, "key_ambiguous_fifth", False))
    return melody_score(pa, ma, pb, mb, fifth_hedge=hedge)


# --------------------------------------------------------------------------- Hooktheory
def _mode_is_minor(intervals: list[int]) -> bool:
    """A Hooktheory scale (step intervals) is minor-like when its third is minor."""
    third = 0
    for step in intervals[:2]:
        third += int(step)
    return third == 3


def hooktheory_frame(annotations: dict, normalization: str = "relative") -> dict | None:
    """One Hooktheory section normalized into our key frame: L1 chord changes and the
    melody's pitch classes with metric classes. Beats are the annotation's meter beats,
    converted to quarter notes."""
    keys = sorted(annotations.get("keys") or [], key=lambda k: k.get("beat", 0))
    if not keys:
        return None
    meters = annotations.get("meters") or [{"beat": 0, "beats_per_bar": 4, "beat_unit": 4}]
    meter = meters[0]
    per_q = 4.0 / float(meter.get("beat_unit") or 4)
    bar = float(meter.get("beats_per_bar") or 4)

    def shift_at(beat: float) -> int:
        cur = keys[0]
        for k in keys:
            if k.get("beat", 0) <= beat + 1e-6:
                cur = k
        mode = MINOR if _mode_is_minor(cur.get("scale_degree_intervals") or []) else MAJOR
        return shift_for(int(cur.get("tonic_pitch_class", 0)) % 12, mode, normalization)

    chg: list[int] = []
    for h in sorted(annotations.get("harmony") or [], key=lambda h: h.get("onset", 0)):
        iv = list(h.get("root_position_intervals") or [])
        if not iv or h.get("root_pitch_class") is None:
            continue
        if (float(h.get("offset", 0)) - float(h.get("onset", 0))) * per_q < 0.75:
            continue
        q = "m" if iv[0] == 3 and (len(iv) < 2 or iv[1] != 3) else ("dim" if iv[:2] == [3, 3] else "")
        tok = l1((int(h["root_pitch_class"]) + shift_at(float(h["onset"]))) % 12, q)
        if not chg or chg[-1] != tok:
            chg.append(tok)
    pcs: list[int] = []
    mets: list[int] = []
    for n in sorted(annotations.get("melody") or [], key=lambda n: n.get("onset", 0)):
        if n.get("pitch_class") is None:
            continue
        onset = float(n["onset"])
        p = 12 * int(n.get("octave", 0)) + int(n["pitch_class"]) + shift_at(onset)
        pos = onset % bar
        met = 0 if abs(pos) < 1e-6 else (1 if abs(pos - round(pos)) < 1e-6 else (2 if abs(onset * per_q * 2 - round(onset * per_q * 2)) < 1e-6 else 3))
        pcs.append(p)
        mets.append(met)
    mp, mm = melody_tokens(pcs, mets)
    return {"chg": chg, "pcs": mp, "met": mm}


def hooktheory_agreement(ident: Any, sections: list[dict], normalization: str | None = None) -> float | None:
    """Mean over annotated sections of how much of each section the song contains:
    local alignment of the section against the whole song, over the section's
    self-alignment (chords and melody averaged). None when nothing is comparable.
    ``sections`` are Hooktheory records (with ``annotations``) or bare annotation dicts."""
    norm = normalization or getattr(ident, "normalization", "relative")
    song_chg = np.asarray(ident.tokens("chg", "L1"), dtype=int)
    song_p = song_m = None
    if ident.melody is not None and len(ident.melody.pitches):
        song_p, song_m = melody_tokens(ident.melody.pitches, ident.melody.met)
    scores = []
    for sec in sections or ():
        ann = sec.get("annotations", sec) if isinstance(sec, dict) else None
        frame = hooktheory_frame(ann, norm) if ann else None
        if frame is None:
            continue
        parts = []
        if len(frame["chg"]) >= 2 and len(song_chg) >= 2:
            x = np.asarray(frame["chg"], dtype=int)
            raw = smith_waterman(CHORD_SUB[x[:, None], song_chg[None, :]], *CHORD_GAP)
            parts.append(min(1.0, raw / (2.0 * len(x))))
        if len(frame["pcs"]) >= 4 and song_p is not None and len(song_p) >= 4:
            raw = smith_waterman(melody_sub(frame["pcs"], frame["met"], song_p, song_m), *MELODY_GAP)
            parts.append(min(1.0, raw / (2.0 * len(frame["pcs"]))))
        if parts:
            scores.append(sum(parts) / len(parts))
    if not scores:
        return None
    return round(sum(scores) / len(scores), 4)
