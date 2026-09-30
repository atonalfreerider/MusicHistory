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

Regions (``resolve``) start from Resonance's key runs and are cleaned with pitch evidence,
because a spurious region moves shared material: Shape of You's hook was stored at two
degrees a fifth apart, Ice Ice Baby flipped between D major and D minor. The evidence for
key ``a`` over a related key ``b`` in a stretch of music is the duration of its pitched
notes whose pitch class only ``a``'s diatonic collection contains, against the duration
only ``b``'s contains (a minor key's leading tone never counts against it). ``a`` is
*decisively* favoured when that is at least 1 % of the notes and twice the other side, and,
for collections one accidental apart (a fifth, or the relative of a fifth), the other side
is at most 2 %. Steps:

A. a run in Resonance's own home key takes the ensemble's home key unless its notes
   decisively favour Resonance's key (Rain & Tears: the B-flat section stays B-flat), and
   runs under 8 bars merge into a neighbour, as before;
B. a run whose notes decisively favour a related key (a fifth away, the parallel mode, or
   one accidental away) that some source proposed for the song (a run, the ensemble home
   or a vote) and whose tonic fits the run about as well (Temperley-Kostka-Payne
   correlation at most 0.1 lower) takes that key (Shape of You's F# minor runs use D# and
   never D: C# minor);
C. the home is re-decided when one key a fifth (or one accidental) from it now covers most
   beats and its profile fits the song about as well (Shape of You: C# minor);
D. a region whose key is a fifth from, the relative or parallel mode of, or one accidental
   from the home key or a neighbour's key merges into it unless it is long and well
   supported: its notes decisively favour its own key and it has at least 8 bars, or they
   favour neither key and it has at least 16 bars. A region whose notes favour the other
   key always merges. Relative modes share their notes, so there the support is a
   profile-correlation difference of 0.1. Shorter regions merge first.

Real lifts (My Sweet Lord's E -> F#, a final whole-step change) are not related keys and
are never merged. The thresholds were tuned on the transcription benchmark (two fan
transcriptions of one song must land in the same frame); see ``tests/identity/test_key.py``.

Normalization: a region with tonic ``t`` is shifted by ``((target - t + 5) % 12) - 5`` in
[-5, 6], target 0 for major and 9 (A) for minor (relative, the default: every song lands
in the C-major/A-minor collection, so a relative major/minor mistake is harmless) or 0 for
every tonic (parallel).
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Iterable
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


def _agree(source: str, vote: Key, k: Key) -> float:
    # Exporters often write "major" whatever the mode: a key signature vouches for the
    # pitch collection, not for the mode (Resonance's KeyContext makes the same call).
    if source == "keysig" and relative_major(vote) == relative_major(k):
        return 1.0
    return mirex(vote, k)


def vote_score(votes: dict[str, Key], k: Key) -> float:
    """Weighted agreement of the votes with key ``k``."""
    return round(sum(WEIGHTS[s] * _agree(s, v, k) for s, v in votes.items() if s in WEIGHTS), 9)


def ensemble(votes: dict[str, Key | None]) -> KeyEstimate:
    """Weighted MIREX-agreement vote over the sources that produced a key."""
    votes = {s: v for s, v in votes.items() if v is not None and s in WEIGHTS}
    if not votes:
        return KeyEstimate(0, MAJOR, 0.0, False, True, {})
    total = sum(WEIGHTS[s] for s in votes)
    order = list(WEIGHTS)

    def score(k: Key) -> float:
        return vote_score(votes, k)

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


def rehome(est: KeyEstimate, home: Key) -> KeyEstimate:
    """``est`` with its home key replaced by ``home`` (a key the region evidence chose): the
    confidence is the votes' agreement with ``home``, and ``ambiguous_fifth`` is set when the
    home moved to a collection one accidental away (the old home is then a real rival)."""
    if home == est.key:
        return est
    total = sum(WEIGHTS[s] for s in est.votes if s in WEIGHTS)
    conf = round(min(1.0, vote_score(est.votes, home) / total), 4) if total > 0 else 0.0
    return KeyEstimate(home[0], home[1], conf, est.ambiguous_fifth or near(home, est.key), est.review, dict(est.votes))


# --------------------------------------------------------------------------- evidence
MAJOR_SCALE = (0, 2, 4, 5, 7, 9, 11)
EVIDENCE_MIN = 0.01        # the favoured key explains at least 1 % of the notes on its own
EVIDENCE_RATIO = 2.0       # ... and at least twice what only the other key explains
EVIDENCE_NEAR_MAX = 0.02   # collections one accidental apart: the other key explains at most 2 %
TONIC_SLACK = 0.1          # profile correlation a relabelled run or a re-decided home may lose
RELATIVE_DELTA = 0.1       # profile-correlation difference that tells relative modes apart
MIN_REGION_BARS = 8        # Resonance's modulations shorter than this merge into a neighbour
SUPPORTED_BARS = 8         # a related region its notes favour survives from this length
NEUTRAL_BARS = 16          # a related region its notes favour neither way survives from this length
MAJORITY = 0.5             # share of the beats that re-decides the home key
RELATED = ("fifth", "parallel", "relative", "adjacent")

# Pitch-class duration histogram of the pitched notes starting in [start, end).
Hist = Callable[[float, float], np.ndarray]


def collection(k: Key) -> frozenset[int]:
    """Pitch classes of the key's diatonic collection (natural minor for minor keys)."""
    r = relative_major(k)
    return frozenset((r + x) % 12 for x in MAJOR_SCALE)


def near(a: Key, b: Key) -> bool:
    """Collections one accidental apart (the keys a fifth apart in the same mode, or relatives of those)."""
    return (relative_major(a) - relative_major(b)) % 12 in (5, 7)


def relation(a: Key, b: Key) -> str | None:
    """'same', 'parallel' (same tonic), 'relative' (same collection), 'fifth' (tonics a fifth
    apart), 'adjacent' (collections one accidental apart) or None (unrelated, e.g. a whole step)."""
    if a == b:
        return "same"
    if a[0] % 12 == b[0] % 12:
        return "parallel"
    if relative_major(a) == relative_major(b):
        return "relative"
    if (a[0] - b[0]) % 12 in (5, 7):
        return "fifth"
    if near(a, b):
        return "adjacent"
    return None


def _neutral(k: Key) -> set[int]:
    """Pitch classes that never count against ``k``: a minor key's leading tone."""
    return {(k[0] + 11) % 12} if k[1] == MINOR else set()


def evidence(h: np.ndarray, a: Key, b: Key) -> tuple[float, float]:
    """(share of the notes only ``a``'s collection explains, share only ``b``'s explains)."""
    tot = float(np.sum(h))
    if tot <= 0:
        return 0.0, 0.0
    ca, cb = collection(a), collection(b)
    only_a = (ca - cb) - _neutral(b)
    only_b = (cb - ca) - _neutral(a)
    return float(sum(h[p] for p in only_a)) / tot, float(sum(h[p] for p in only_b)) / tot


def decisive(h: np.ndarray, a: Key, b: Key) -> bool:
    """The notes clearly favour key ``a`` over key ``b`` (see the module docstring)."""
    ea, eb = evidence(h, a, b)
    if ea < EVIDENCE_MIN or ea < EVIDENCE_RATIO * eb:
        return False
    return not near(a, b) or eb <= EVIDENCE_NEAR_MAX


def profile_fit(h: np.ndarray, k: Key) -> float:
    """Pearson correlation of a histogram with the key's Temperley-Kostka-Payne profile (0 if flat)."""
    if np.sum(h) <= 0 or np.allclose(h, h[0]):
        return 0.0
    prof = np.array(TKP_MINOR if k[1] == MINOR else TKP_MAJOR)
    return float(np.corrcoef(h, np.roll(prof, k[0]))[0, 1])


def note_histogram(notes: Iterable, *, drums: int = 10, cap: float = 4.0) -> Hist:
    """``Hist`` over slim note rows ``[beat, length, pitch, track, channel, velocity]``: the
    duration (capped at ``cap`` beats, so pads do not drown the tune) of every pitched note by
    pitch class, counted where the note starts."""
    rows = sorted((float(n[0]), min(max(float(n[1]), 0.0), cap), int(n[2]) % 12) for n in notes if n[4] != drums)
    onsets = [r[0] for r in rows]
    cum = np.zeros((len(rows) + 1, 12))
    for i, (_, d, pc) in enumerate(rows):
        cum[i + 1] = cum[i]
        cum[i + 1, pc] += d

    def hist(start: float, end: float) -> np.ndarray:
        i = bisect.bisect_left(onsets, start - 1e-6)
        j = bisect.bisect_left(onsets, end - 1e-6)
        return cum[max(i, j)] - cum[i]
    return hist


# --------------------------------------------------------------------------- regions
@dataclass
class KeyRegion:
    start: float
    end: float
    tonic: int
    mode: str
    shift: int            # applied (per the chosen normalization)
    shift_parallel: int


def _join(runs: list[list]) -> list[list]:
    out: list[list] = []
    for r in runs:
        if out and out[-1][2] == r[2]:
            out[-1][1] = r[1]
        else:
            out.append(list(r))
    return out


def _merge_short(runs: list[list], min_beats: float) -> list[list]:
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
        runs = _join(runs)
    return runs


def _relabel(runs: list[list], proposals: set[Key], home: Key, hist: Hist) -> None:
    """Step B: a run takes a related proposed key its notes decisively favour (tonic fit permitting)."""
    labels = {r[2] for r in runs}
    for r in runs:
        h = hist(r[0], r[1])
        best = None
        for k in sorted(proposals):
            if relation(r[2], k) not in ("fifth", "parallel", "adjacent") or not decisive(h, k, r[2]):
                continue
            if profile_fit(h, k) < profile_fit(h, r[2]) - TONIC_SLACK:
                continue
            ea, eb = evidence(h, k, r[2])
            rank = (round(ea - eb, 9), k[1] == r[2][1], k in labels, k == home)
            if best is None or rank > best[0]:
                best = (rank, k)
        if best is not None:
            r[2] = best[1]


def _keep(run: list, target: Key, hist: Hist | None, beats_per_bar: float) -> bool:
    """Step D: does a region survive next to a related key (home or neighbour)?"""
    rel = relation(run[2], target)
    if rel not in RELATED:
        return True
    h = hist(run[0], run[1]) if hist is not None else None
    if h is None or np.sum(h) <= 0:
        sup = con = False
    elif rel == "relative":
        d = profile_fit(h, run[2]) - profile_fit(h, target)
        sup, con = d >= RELATIVE_DELTA, -d >= RELATIVE_DELTA
    else:
        sup, con = decisive(h, run[2], target), decisive(h, target, run[2])
    if con:
        return False
    bars = (run[1] - run[0]) / beats_per_bar
    return bars >= (SUPPORTED_BARS if sup else NEUTRAL_BARS) - 1e-6


def resolve(key_runs: list, home: Key, resonance: Key | None, end_beat: float, *, beats_per_bar: float = 4.0,
            normalization: str = "relative", hist: Hist | None = None,
            proposals: Iterable[Key | None] = ()) -> tuple[Key, list[KeyRegion]]:
    """Home key and key regions from Resonance's runs (``[start, end, tonic, minor]``), the
    ensemble's home key and, when given, the notes (``hist``) and the keys other sources
    proposed (``proposals``: the votes). Steps A-D of the module docstring; without notes only
    step A's relabel, the 8-bar merge and the length rule of step D apply."""
    bpb = float(beats_per_bar) if beats_per_bar and beats_per_bar > 0 else 4.0
    runs: list[list] = []
    for start, end, tonic, minor in key_runs or ():
        k: Key = (int(tonic) % 12, MINOR if minor else MAJOR)
        # A: Resonance's home runs take the ensemble's home key unless their notes say otherwise.
        if resonance is not None and k == resonance and k != home:
            if hist is None or not decisive(hist(float(start), float(end)), k, home):
                k = home
        if runs and runs[-1][2] == k:
            runs[-1][1] = float(end)
        else:
            runs.append([float(start), float(end), k])
    runs = _merge_short(runs, MIN_REGION_BARS * bpb - 1e-6)
    if not runs:
        runs = [[0.0, float(end_beat), home]]
    runs[0][0] = 0.0
    runs[-1][1] = max(runs[-1][1], float(end_beat))
    if hist is not None:
        # B: relabel runs whose notes contradict their key.
        _relabel(runs, {r[2] for r in runs} | {home} | {k for k in proposals if k is not None}, home, hist)
        runs = _join(runs)
        # C: re-decide the home key when a key one fifth away now covers most of the song.
        beats: dict[Key, float] = {}
        for r in runs:
            beats[r[2]] = beats.get(r[2], 0.0) + r[1] - r[0]
        top, n = max(sorted(beats.items()), key=lambda kv: (kv[1], kv[0] == home))
        if top != home and n > MAJORITY * sum(beats.values()) and relation(top, home) in ("fifth", "adjacent"):
            h = hist(0.0, float(runs[-1][1]) + 1.0)
            if profile_fit(h, top) >= profile_fit(h, home) - TONIC_SLACK:
                home = top
    # D: merge related regions into the home key or a neighbour unless long and well supported.
    while len(runs) > 1:
        changed = False
        for i in sorted(range(len(runs)), key=lambda j: (runs[j][1] - runs[j][0], j)):
            r = runs[i]
            if r[2] == home:
                continue
            for t in [home] + [runs[j][2] for j in (i - 1, i + 1) if 0 <= j < len(runs)]:
                if not _keep(r, t, hist, bpb):
                    r[2] = t
                    changed = True
                    break
            if changed:
                break
        if not changed:
            break
        runs = _join(runs)
    return home, [KeyRegion(round(s, 4), round(e, 4), k[0], k[1], shift_for(k[0], k[1], normalization),
                            shift_for(k[0], k[1], "parallel")) for s, e, k in runs]


def regions(key_runs: list, home: Key, resonance: Key | None, end_beat: float, *, beats_per_bar: float = 4.0,
            normalization: str = "relative", hist: Hist | None = None,
            proposals: Iterable[Key | None] = ()) -> list[KeyRegion]:
    """The key regions of ``resolve`` (which also returns the possibly re-decided home key)."""
    return resolve(key_runs, home, resonance, end_beat, beats_per_bar=beats_per_bar, normalization=normalization,
                   hist=hist, proposals=proposals)[1]


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
