"""Home key by ensemble vote, key regions, and the normalization shift per region.

Why an ensemble (mir-methods.md §4.2): Resonance's chord Viterbi is the best single source
for pop (it treats bVII, bVI and iv as cheap borrowings), but it and the profile methods
fail in different ways, and a MIDI key signature is informative only when it is not the
default C major (Raffel & Ellis 2016). Weights (DESIGN.md §7):

    Resonance home region (duration-weighted mode of key_runs)   0.45
    Temperley-Kostka-Payne profile, bass counted double          0.25
    Krumhansl-Kessler profile                                    0.10
    original MIDI key signature (only if not C major)            0.20
    root of the final section's last chord                       +0.10

Each source votes for one key; a key's score is the MIREX-weighted agreement of all votes
with it (same 1, fifth 0.5, relative 0.3, parallel 0.2), so near-misses support each other.
A key signature counts fully for both keys of its pitch collection (exporters often write
"major" whatever the mode). Ties go to the key more sources voted for. The confidence is
the winner's score over the weights present.

Regions: Resonance's runs are kept, but a run in Resonance's own home key takes the
ensemble's home key, so a Resonance error on the home key does not leak into the tokens.

Normalization: a region with tonic ``t`` is shifted by ``((target - t + 5) % 12) - 5`` in
[-5, 6], target 0 for major and 9 (A) for minor (relative, the default: every song lands
in the C-major/A-minor collection, so a relative major/minor mistake is harmless) or 0 for
every tonic (parallel).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import numpy as np

MAJOR, MINOR = "major", "minor"
WEIGHTS = {"resonance": 0.45, "tkp": 0.25, "kk": 0.10, "keysig": 0.20, "final_chord": 0.10}

# Profiles as in music21 (checked in mir-methods.md §4.2); index 0 = tonic.
TKP_MAJOR = [.748, .060, .488, .082, .670, .460, .096, .715, .104, .366, .057, .400]
TKP_MINOR = [.712, .084, .474, .618, .049, .460, .105, .747, .404, .067, .133, .330]
KK_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KK_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
FLAT_MAJOR = {5, 10, 3, 8, 1, 6}      # F Bb Eb Ab Db Gb major
FLAT_MINOR = {2, 7, 0, 5, 10, 3}      # D G C F Bb Eb minor

Key = tuple[int, str]  # (tonic pitch class C = 0, 'major' | 'minor')


def key_name(tonic: int, mode: str) -> str:
    flats = FLAT_MINOR if mode == MINOR else FLAT_MAJOR
    names = FLAT_NAMES if tonic % 12 in flats else SHARP_NAMES
    return f"{names[tonic % 12]} {mode}"


def wrap_shift(semitones: int) -> int:
    """Map any semitone offset to the equivalent shift in [-5, 6]."""
    return ((int(semitones) + 5) % 12) - 5


def shift_for(tonic: int, mode: str, normalization: str = "relative") -> int:
    """Semitones added to native pitches so this key lands on the target key."""
    target = 9 if (mode == MINOR and normalization == "relative") else 0
    return wrap_shift(target - tonic)


def relative_major(k: Key) -> int:
    """Tonic of the major key sharing this key's pitch collection."""
    return (k[0] + 3) % 12 if k[1] == MINOR else k[0] % 12


def mirex(est: Key, ref: Key) -> float:
    if est == ref:
        return 1.0
    if est[1] == ref[1] and (est[0] - ref[0]) % 12 in (5, 7):
        return 0.5
    if est[1] != ref[1] and relative_major(est) == relative_major(ref):
        return 0.3
    if est[0] == ref[0]:
        return 0.2
    return 0.0


# --------------------------------------------------------------------------- votes
def resonance_home(key_runs: list) -> Key | None:
    """Duration-weighted mode of Resonance's region keys (``key_runs`` rows
    ``[start, end, tonic, minor]``)."""
    w: dict[Key, float] = {}
    for start, end, tonic, minor in key_runs or ():
        if tonic is None or tonic < 0:
            continue
        k = (int(tonic), MINOR if minor else MAJOR)
        w[k] = w.get(k, 0.0) + max(0.0, float(end) - float(start))
    if not w:
        return None
    return max(w.items(), key=lambda kv: (kv[1], -kv[0][0], kv[0][1]))[0]


def profile_key(hist: np.ndarray, major: list[float], minor: list[float]) -> Key | None:
    """Key whose rotated profile correlates best (Pearson) with a 12-bin histogram."""
    if hist.sum() <= 0 or np.allclose(hist, hist[0]):
        return None
    best, best_r = None, -2.0
    for mode, prof in ((MAJOR, np.array(major)), (MINOR, np.array(minor))):
        for tonic in range(12):
            r = float(np.corrcoef(hist, np.roll(prof, tonic))[0, 1])
            if r > best_r + 1e-12:
                best, best_r = (tonic, mode), r
    return best


def keysig_vote(features: dict | None) -> Key | None:
    """The original file's key signature (DESIGN §5 rows ``[beat, sharps_flats, minor]``),
    the one covering most of the file; ignored when it is the default C major."""
    rows = (features or {}).get("key_signatures") or []
    if not rows:
        return None
    end = float((features or {}).get("end_beat") or 0.0)
    rows = sorted(rows, key=lambda r: r[0])
    span: dict[Key, float] = {}
    for i, (beat, sf, minor) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else max(end, beat + 1)
        tonic = (7 * int(sf) + (9 if minor else 0)) % 12
        k = (tonic, MINOR if minor else MAJOR)
        span[k] = span.get(k, 0.0) + max(0.0, nxt - beat) + 1e-6
    k = max(span.items(), key=lambda kv: kv[1])[0]
    return None if k == (0, MAJOR) else k


def final_chord_vote(slim: dict) -> Key | None:
    """Root (and mode from the triad) of the final section's last sounding chord."""
    chords = [c for c in slim.get("chords") or () if c[2] >= 0 and c[3] != "dim"]
    if not chords:
        return None
    sections = slim.get("sections") or []
    if sections:
        last = sections[-1]
        inside = [c for c in chords if c[1] > last["start"] + 1e-6 and c[0] < last["end"] - 1e-6]
        chords = inside or chords
    c = chords[-1]
    return int(c[2]), MINOR if c[3] in ("m", "m7") else MAJOR


# --------------------------------------------------------------------------- ensemble
@dataclass
class KeyEstimate:
    tonic: int
    mode: str
    confidence: float
    ambiguous_fifth: bool
    review: bool
    votes: dict[str, Key] = field(default_factory=dict)

    @property
    def key(self) -> Key:
        return self.tonic, self.mode


def ensemble(votes: dict[str, Key | None]) -> KeyEstimate:
    """Weighted MIREX-agreement vote over the sources that produced a key."""
    votes = {s: v for s, v in votes.items() if v is not None and s in WEIGHTS}
    if not votes:
        return KeyEstimate(0, MAJOR, 0.0, False, True, {})
    total = sum(WEIGHTS[s] for s in votes)
    order = list(WEIGHTS)

    def agree(source: str, vote: Key, k: Key) -> float:
        # Exporters often write "major" whatever the mode: a key signature vouches for the
        # pitch collection, not for the mode (Resonance's KeyContext makes the same call).
        if source == "keysig" and relative_major(vote) == relative_major(k):
            return 1.0
        return mirex(vote, k)

    def score(k: Key) -> float:
        return round(sum(WEIGHTS[s] * agree(s, v, k) for s, v in votes.items()), 9)

    # Ties go to the key more sources voted for, then to the more trusted source's vote.
    candidates = sorted(set(votes.values()), key=lambda k: (
        -score(k), -sum(1 for v in votes.values() if v == k), min(order.index(s) for s, v in votes.items() if v == k)))
    winner = candidates[0]
    ambiguous = review = False
    rivals = [k for k in candidates[1:] if relative_major(k) != relative_major(winner)]
    if rivals:
        rival = rivals[0]
        # Only a disagreement carried by a real source (>= 0.2 of weight) is flagged.
        support = sum(WEIGHTS[s] for s, v in votes.items() if relative_major(v) == relative_major(rival))
        if support >= 0.2 - 1e-9:
            if (relative_major(rival) - relative_major(winner)) % 12 in (5, 7):
                ambiguous = True
            else:
                review = True
    return KeyEstimate(winner[0], winner[1], round(min(1.0, score(winner) / total), 4), ambiguous, review, votes)


# --------------------------------------------------------------------------- regions
@dataclass
class KeyRegion:
    start: float
    end: float
    tonic: int
    mode: str
    shift: int            # applied (per the chosen normalization)
    shift_parallel: int


def regions(key_runs: list, home: Key, resonance: Key | None, end_beat: float, *, beats_per_bar: float = 4.0,
            normalization: str = "relative") -> list[KeyRegion]:
    """Key regions from Resonance's runs (modulations of at least 8 bars). Runs in
    Resonance's home key take the ensemble's home key; shorter runs merge into a neighbour."""
    runs: list[list] = []
    for start, end, tonic, minor in key_runs or ():
        k: Key = (int(tonic), MINOR if minor else MAJOR)
        if resonance is not None and k == resonance:
            k = home
        if runs and (runs[-1][2], runs[-1][3]) == k:
            runs[-1][1] = float(end)
        else:
            runs.append([float(start), float(end), k[0], k[1]])
    min_beats = 8 * beats_per_bar - 1e-6
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, r in enumerate(runs):
            if r[1] - r[0] < min_beats:
                if i > 0:
                    runs[i - 1][1] = r[1]
                else:
                    runs[1][0] = r[0]
                del runs[i]
                changed = True
                break
        merged: list[list] = []
        for r in runs:
            if merged and (merged[-1][2], merged[-1][3]) == (r[2], r[3]):
                merged[-1][1] = r[1]
            else:
                merged.append(r)
        runs = merged
    if not runs:
        runs = [[0.0, float(end_beat), home[0], home[1]]]
    runs[0][0] = 0.0
    runs[-1][1] = max(runs[-1][1], float(end_beat))
    return [KeyRegion(round(s, 4), round(e, 4), t, m, shift_for(t, m, normalization), shift_for(t, m, "parallel"))
            for s, e, t, m in runs]


class ShiftMap:
    """Beat -> normalization shift of the region containing it."""

    def __init__(self, regs: list[KeyRegion]) -> None:
        self.regions = regs
        self.starts = [r.start for r in regs]

    def region_at(self, beat: float) -> KeyRegion:
        i = bisect.bisect_right(self.starts, beat + 1e-6) - 1
        return self.regions[max(0, i)]

    def shift_at(self, beat: float) -> int:
        return self.region_at(beat).shift
