"""Is a shared identity audible in a preview? Checks on the per-beat chord and bass readings
of ``audio.measure`` (a simple chord-chroma estimate, not a transcription).

Families store their chords as roman numerals in the C-major/A-minor frame (DESIGN §8b), so a
recording in key (t, mode) maps a frame pitch class p to ``p + t`` (major) or ``p + t - 9``
(minor). Checks, by family kind:

* loops and named schemas (cyclic, rotation-invariant): one full cycle of changes in a row
  (a 2-chord vamp needs A-B-A-B, a 3-chord loop A-B-C-A); cycles of 4+ chords compare roots
  only (the design's E vs Em tolerance);
* cadences (``ii-V-I``, ``I-IV-V-I``, ``iio-V-i``): the changes in that order;
* ``12-bar blues``: 12 bars in a row whose majority roots follow I-I-I-I-IV-IV-I-I-V-IV-I-I
  (quick-change IV in bar 2, V in bar 10 and a V turnaround in bar 12 allowed) in at least
  10 of 12 bars including bars 1, 5 and 9 (I, IV, V), on bars of 2, 4 or 8 grid beats and any
  phase;
* ``descending chromatic bass``: the four bass notes in a row on the per-beat bass reading;
* strong ``exact chord passage``: both clips share a run of at least 4 changes in the key
  frame; ``exact melody passage`` / ``exact bass riff``: the best 8-beat key-normalized chroma
  match between the two clips, ranked against the same clip matched with random other
  previews (harmonic similarity only; a melody cannot be read off a full-mix chroma).

Each clip check reports ``in_key`` (at the measured key) and ``any_key`` (some transposition,
i.e. the pattern is there but the key reading differs).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from ..identity.key import MINOR

DEGREE = {"I": 0, "II": 2, "III": 4, "IV": 5, "V": 7, "VI": 9, "VII": 11}
ARABIC = {"1": 0, "2": 2, "3": 4, "4": 5, "5": 7, "6": 9, "7": 11}
BLUES_BARS = [{0}, {0, 5}, {0}, {0}, {5}, {5}, {0}, {0}, {7}, {5, 7}, {0}, {0, 7}]
BLUES_MIN = 10
BLUES_REQUIRED = (0, 4, 8)          # bars 1, 5 and 9 (I, IV, V) define the form
CHORD_RUN_MIN = 4
SIM_WINDOW = 8


def frame_shift(tonic: int, mode: str) -> int:
    """Add to a C-major/A-minor frame pitch class to get the recording's pitch class."""
    return (tonic - 9) % 12 if mode == MINOR else tonic % 12


def parse_roman(roman: str) -> list[tuple[int, str]]:
    """'vi-IV-I-V' -> [(9,'min'), (5,'maj'), (0,'maj'), (7,'maj')]; 'viio' is 'dim'."""
    out = []
    for tok in roman.split("-"):
        m = re.fullmatch(r"([b#]*)([IViv]+)(o|\+)?.*", tok.strip())
        if not m:
            raise ValueError(f"not a roman numeral: {tok!r}")
        acc, num, suf = m.groups()
        pc = DEGREE[num.upper()] + acc.count("#") - acc.count("b")
        q = "dim" if suf == "o" else ("maj" if num.isupper() else "min")
        out.append((pc % 12, q))
    return out


def parse_degrees(text: str) -> list[int]:
    """'b7-6-b6-5' -> [10, 9, 8, 7]."""
    out = []
    for tok in text.split("-"):
        m = re.fullmatch(r"([b#]*)([1-7])", tok.strip())
        if not m:
            raise ValueError(f"not a scale degree: {tok!r}")
        out.append((ARABIC[m.group(2)] + m.group(1).count("#") - m.group(1).count("b")) % 12)
    return out


def collapse(labels: list[int], min_beats: int = 1) -> list[tuple[int, int]]:
    """Per-beat labels -> [(label, beats)] runs, dropping silence and runs shorter than
    ``min_beats`` (merged away), then re-joining equal neighbours."""
    runs: list[list[int]] = []
    for lab in labels:
        if lab < 0:
            continue
        if runs and runs[-1][0] == lab:
            runs[-1][1] += 1
        else:
            runs.append([lab, 1])
    if min_beats > 1:
        runs = [r for r in runs if r[1] >= min_beats]
        joined: list[list[int]] = []
        for r in runs:
            if joined and joined[-1][0] == r[0]:
                joined[-1][1] += r[1]
            else:
                joined.append(list(r))
        runs = joined
    return [(a, b) for a, b in runs]


def _chord_ok(lab: int, pc: int, q: str, roots_only: bool) -> bool:
    root, minor = divmod(lab, 2)
    if root != pc % 12:
        return False
    if roots_only or q == "dim":
        return True
    return (q == "min") == bool(minor)


def _seq_find(labels: list[int], pattern: list[tuple[int, str]], roots_only: bool) -> bool:
    n = len(pattern)
    for i in range(len(labels) - n + 1):
        if all(_chord_ok(labels[i + k], *pattern[k], roots_only) for k in range(n)):
            return True
    return False


def _sequences(chords: list[int]) -> list[list[int]]:
    """The chord sequence at two smoothing levels (all changes; changes of 2+ beats)."""
    return [[a for a, _ in collapse(chords, 1)], [a for a, _ in collapse(chords, 2)]]


def find_cycle(chords: list[int], cycle: list[tuple[int, str]], shift: int) -> bool:
    L = len(cycle)
    if L == 0:
        return False
    need = 4 if L == 2 else (L + 1 if L == 3 else L)
    roots_only = L >= 4
    moved = [((p + shift) % 12, q) for p, q in cycle]
    for seq in _sequences(chords):
        for r in range(L):
            rot = moved[r:] + moved[:r]
            pattern = [rot[k % L] for k in range(need)]
            if _seq_find(seq, pattern, roots_only):
                return True
    return False


def find_progression(chords: list[int], prog: list[tuple[int, str]], shift: int) -> bool:
    moved = [((p + shift) % 12, q) for p, q in prog]
    return any(_seq_find(seq, moved, roots_only=False) for seq in _sequences(chords))


def find_blues(chords: list[int], shift: int) -> bool:
    roots = [c // 2 if c >= 0 else -1 for c in chords]
    for bar in (4, 2, 8):
        for phase in range(bar):
            bars = []
            for i in range(phase, len(roots) - bar + 1, bar):
                cnt = Counter(r for r in roots[i:i + bar] if r >= 0)
                bars.append(cnt.most_common(1)[0][0] if cnt else -1)
            for i in range(len(bars) - 11):
                ok = [bars[i + k] >= 0 and (bars[i + k] - shift) % 12 in BLUES_BARS[k] for k in range(12)]
                if sum(ok) >= BLUES_MIN and all(ok[k] for k in BLUES_REQUIRED):
                    return True
    return False


def find_bass_line(bass: list[int], degrees: list[int], shift: int) -> bool:
    seq = [a for a, _ in collapse(bass, 1)]
    target = [(d + shift) % 12 for d in degrees]
    n = len(target)
    return any(seq[i:i + n] == target for i in range(len(seq) - n + 1))


@dataclass
class ClipCheck:
    checked: bool
    in_key: bool = False
    any_key: bool = False
    note: str = ""


def check_clip(kind: str, label: str, roman: str | None, chords: list[int], bass: list[int],
               tonic: int, mode: str) -> ClipCheck:
    """Is the family's pattern audible in one clip (see the module docstring)?"""
    shift = frame_shift(tonic, mode)
    try:
        if label == "12-bar blues":
            fn = lambda s: find_blues(chords, s)                      # noqa: E731
        elif label.startswith("descending chromatic bass"):
            degrees = parse_degrees(roman or label.rsplit(" ", 1)[-1])
            fn = lambda s: find_bass_line(bass, degrees, s)           # noqa: E731
        elif kind in ("loop", "schema") and roman:
            cyc = parse_roman(roman)
            fn = lambda s: find_cycle(chords, cyc, s)                 # noqa: E731
        elif kind == "progression" and roman:
            prog = parse_roman(roman)
            fn = lambda s: find_progression(chords, prog, s)          # noqa: E731
        else:
            return ClipCheck(False, note="pair check" if kind == "strong" else "no pattern")
    except ValueError as exc:
        return ClipCheck(False, note=str(exc))
    in_key = fn(shift)
    any_key = in_key or any(fn(s) for s in range(12) if s != shift)
    return ClipCheck(True, in_key, any_key)


# --------------------------------------------------------------------------- pair checks
def relative_chords(chords: list[int], tonic: int, mode: str) -> list[int]:
    """Collapsed chord labels moved into the C-major/A-minor frame."""
    shift = frame_shift(tonic, mode)
    return [(((a // 2) - shift) % 12) * 2 + a % 2 for a, _ in collapse(chords, 1)]


def longest_common_run(a: list[int], b: list[int]) -> int:
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def best_window_similarity(A: np.ndarray, B: np.ndarray, window: int = SIM_WINDOW) -> float:
    """Max over window pairs of the mean cosine similarity of ``window`` consecutive beat
    chroma columns (both already in a common key frame)."""
    if A.shape[1] < window or B.shape[1] < window:
        return 0.0
    An = A / np.maximum(np.linalg.norm(A, axis=0, keepdims=True), 1e-9)
    Bn = B / np.maximum(np.linalg.norm(B, axis=0, keepdims=True), 1e-9)
    S = An.T @ Bn                                              # (na, nb) beat similarities
    na, nb = S.shape
    best = 0.0
    for off in range(-(na - window), nb - window + 1):
        d = np.diagonal(S, offset=off)
        if len(d) < window:
            continue
        c = np.convolve(d, np.ones(window) / window, mode="valid")
        best = max(best, float(c.max()))
    return best


def roll_to_frame(chroma: np.ndarray, tonic: int, mode: str) -> np.ndarray:
    return np.roll(chroma, -frame_shift(tonic, mode), axis=0)


@dataclass
class PairCheck:
    kind: str                    # 'chord_run' | 'chroma_similarity' | 'clips'
    passed: bool | None
    value: float = 0.0
    detail: dict = field(default_factory=dict)
