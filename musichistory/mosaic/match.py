"""Note-for-note matching of melody spans, invariant to transposition and tempo.

A **sequence** (``Seq``) is a song's sung notes in beats of its own grid (onset, end, MIDI
pitch, and the unrounded median pitch ``fpitch``). A source sequence is laid onto a target by
one **alignment**: a fold ``f`` (target beats per source beat: 0.5, 1 or 2 - the tempo-octave
folds), an ``offset`` (target beat of source beat 0) and a ``transpose`` (semitones, octaves
included: the audio is shifted by at most ``MAX_SHIFT`` semitones and heard at most
``MAX_OCTAVE`` octaves from the target). The heard **tempo ratio** (source seconds per output
second, ``ibi_s / (f * ibi_t)``) must lie in ``TEMPO_RANGE``.

Under an alignment a target note and a source note **match** when the transposed pitch is
equal (the unrounded pitches within ``PITCH_TOL`` semitones, so a note sung near a rounding
boundary does not flip), the onsets are within ``ONSET_TOL`` beats and the durations are
similar (ratio within ``DUR_RATIO`` or within ``DUR_ABS`` beats); matching is one-to-one, in
order.

A **piece** is a run of consecutive target notes ``a..b`` (both matched) sung by the run of
consecutive source notes between their partners. Its match rate is ``matched / max(target
notes, source notes)`` in the run, so a source that sings extra notes, or misses some, scores
lower; notes of either melody left unmatched that are shorter than ``ORNAMENT`` beats, or repeat
the note before them (another syllable on the same pitch), are ornaments and do not count. Pieces need ``MIN_PIECE`` matched notes, a span of at least ``min_beats`` target beats
(one bar, from the first note's onset until the last note gives way to the next, ``held_until``:
long enough to tell which song sings) and a rate of at least ``MIN_RATE`` - ``LONG_RATE`` for a span of ``long_beats`` or more
(two bars), so a much longer piece may miss a few more notes.

**Search** (``Index``, ``find_pieces``): every ``NGRAM``-note n-gram (3) of every song's
pitch-changing notes (a repeated pitch is skipped, ``contour_notes``) is indexed by its pitch
intervals (and, with ``RATIO_CLASS`` > 0, its inter-onset ratios in log2
classes; off by default, the rhythm is checked by the fold instead): a key that does not change
under transposition or tempo. The target loop's n-grams look up their seeds (about 75 000
notes, 30-60 per loop: a few thousand seeds a target); each seed gives an alignment (the fold
whose ratio fits the seed's span within ``FOLD_TOL``, offset and transposition from the seed's
notes), alignments are deduplicated, matched against the whole source sequence, the offset is
re-estimated from the matched pairs (median) and matched again; every valid piece of the
alignment is kept (``pieces_of``: all runs between two matched notes at once, vectorized).

Only pitches and times: no words.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np

ONSET_TOL = 0.125            # beats
PITCH_TOL = 0.5              # semitones between the unrounded pitches of matching notes
DUR_RATIO = (0.4, 2.5)
DUR_ABS = 0.5                # beats
FOLDS = (0.5, 1.0, 2.0)
FOLD_TOL = 0.2               # |log2(span ratio / fold)| allowed for a seed (beat-grid wobble)
TEMPO_RANGE = (0.66, 1.5)
NGRAM = 3
RATIO_CLASS = 0.0            # IOI-ratio classes per octave in the seed key (0: pitch intervals only)
MIN_PIECE = 4
MIN_RATE = 0.7
ORNAMENT = 0.25              # target beats: a shorter extra source note is an ornament, not a wrong note
LONG_RATE = 0.6              # ... the rate a long piece (``long_beats``) needs
MAX_SHIFT = 6                # audio transposition (semitones), octaves folded ...
MAX_OCTAVE = 1               # ... and the piece heard at most this many octaves from the target


@dataclass
class Seq:
    """A melody as notes in beats of its own grid."""

    work_id: str
    on: np.ndarray
    off: np.ndarray
    pitch: np.ndarray
    ibi: float = 0.5                   # seconds per beat (median)
    fpitch: np.ndarray | None = None   # unrounded median pitch per note (None: the rounded one)

    def __post_init__(self):
        self.on = np.asarray(self.on, dtype=float)
        self.off = np.asarray(self.off, dtype=float)
        self.pitch = np.asarray(self.pitch, dtype=int)
        self.fpitch = self.pitch.astype(float) if self.fpitch is None else np.asarray(self.fpitch, dtype=float)
        order = np.argsort(self.on, kind="stable")
        self.on, self.off = self.on[order], self.off[order]
        self.pitch, self.fpitch = self.pitch[order], self.fpitch[order]

    def __len__(self) -> int:
        return len(self.on)

    def window(self, lo: float, hi: float) -> "Seq":
        """Notes with onsets in [lo, hi), re-based so ``lo`` is beat 0."""
        sel = (self.on >= lo) & (self.on < hi)
        return Seq(self.work_id, self.on[sel] - lo, self.off[sel] - lo, self.pitch[sel], self.ibi, self.fpitch[sel])


@dataclass(frozen=True)
class Alignment:
    work_id: str
    fold: float
    offset: float
    transpose: int


@dataclass
class Piece:
    work_id: str
    a: int                             # first target note (index into the target loop's notes)
    b: int                             # last target note (inclusive)
    sa: int                            # first source note
    sb: int                            # last source note (inclusive)
    fold: float
    offset: float
    transpose: int                     # target pitch = source pitch + transpose
    matched: int
    n_target: int
    n_source: int
    tempo_ratio: float
    weight: float = 0.0
    pairs: list[tuple[int, int]] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.matched / max(self.n_target, self.n_source, 1)

    @property
    def shift(self) -> int:
        """Audio transposition in -6..6 (the octave folded away)."""
        return fold_shift(self.transpose)

    @property
    def octave(self) -> int:
        return (self.transpose - self.shift) // 12


def fold_shift(transpose: int) -> int:
    """The audio shift (-6..6) of a transposition, its octaves folded away; a tritone tie
    keeps the fewer octaves (18 -> +6 an octave up, not -6 two octaves up)."""
    t = int(transpose)
    k = (abs(t) + 5) // 12
    return t - 12 * (k if t >= 0 else -k)


def tempo_ratio(ibi_source: float, ibi_target: float, fold: float) -> float:
    """Source seconds per output second when one source beat spans ``fold`` target beats."""
    return float(ibi_source / (fold * ibi_target))


def allowed_folds(ibi_source: float, ibi_target: float) -> list[float]:
    lo, hi = TEMPO_RANGE
    return [f for f in FOLDS if lo - 1e-9 <= tempo_ratio(ibi_source, ibi_target, f) <= hi + 1e-9]


# --------------------------------------------------------------------------- matching
def match_notes(target: Seq, source: Seq, fold: float, offset: float, transpose: int,
                tol: float = ONSET_TOL) -> np.ndarray:
    """Partner (source note index or -1) of every target note under the alignment."""
    s_on = fold * source.on + offset
    s_dur = fold * (source.off - source.on)
    s_pitch = source.fpitch + transpose
    out = np.full(len(target), -1, dtype=int)
    if not len(source) or not len(target):
        return out
    lo = np.searchsorted(s_on, target.on - tol, side="left")
    hi = np.searchsorted(s_on, target.on + tol, side="right")
    t_dur = target.off - target.on
    last = -1
    for i in np.flatnonzero(hi > lo):
        best, best_d = -1, 1e9
        for j in range(max(int(lo[i]), last + 1), int(hi[i])):
            if abs(s_pitch[j] - target.fpitch[i]) > PITCH_TOL:
                continue
            d = abs(s_on[j] - target.on[i])
            if d < best_d and durations_similar(s_dur[j], t_dur[i]):
                best, best_d = j, d
        if best >= 0:
            out[i] = best
            last = best
    return out


def durations_similar(a: float, b: float) -> bool:
    if abs(a - b) <= DUR_ABS:
        return True
    r = a / max(b, 1e-6)
    return DUR_RATIO[0] <= r <= DUR_RATIO[1]


def refine_offset(target: Seq, source: Seq, fold: float, partner: np.ndarray, offset: float) -> float:
    ok = partner >= 0
    if ok.sum() < 2:
        return offset
    return float(np.median(target.on[ok] - fold * source.on[partner[ok]]))


def piece_weight(matched: int, span: int, *, length_bonus: float, miss_cost: float, piece_cost: float) -> float:
    """``(matched - miss_cost * misses) * (1 + length_bonus * (matched - 1)) - piece_cost``: every
    note is worth more in a longer piece, every miss costs as much as a matched note earns (so the
    note-for-note match still ranks first), and every piece has a fixed cost."""
    return (matched - miss_cost * (span - matched)) * (1.0 + length_bonus * (matched - 1)) - piece_cost


def held_until(seq: Seq, max_rest: float = 1.0) -> np.ndarray:
    """Per note, the beat until which it holds the melody: the next note's onset (a rest of at most
    ``max_rest`` beats counts with the note before it), at least its own end."""
    nxt = np.r_[seq.on[1:], seq.off[-1:]] if len(seq) else np.zeros(0)
    return np.maximum(seq.off, np.minimum(nxt, seq.off + max_rest))


def pieces_of(target: Seq, source: Seq, al: Alignment, partner: np.ndarray, *, ibi_target: float,
              min_piece: int = MIN_PIECE, min_rate: float = MIN_RATE, min_beats: float = 0.0,
              long_beats: float = float("inf"), long_rate: float = LONG_RATE, **weights) -> list[Piece]:
    """Every valid piece (run of target notes between two matched ones) of an alignment."""
    m = np.flatnonzero(partner >= 0)
    if len(m) < min_piece:
        return []
    k = len(m)
    p, q = np.meshgrid(np.arange(k), np.arange(k), indexing="ij")
    a, b = m[p], m[q]
    sa, sb = partner[a], partner[b]
    matched = q - p + 1
    t_same = np.r_[False, np.abs(np.diff(target.fpitch)) <= PITCH_TOL]
    t_cost = (((target.off - target.on) >= ORNAMENT) & ~t_same).astype(int)
    tsum = np.r_[0, np.cumsum(t_cost)]
    tm = np.r_[0, np.cumsum(t_cost[m])]
    n_t = matched + np.maximum((tsum[b + 1] - tsum[a]) - (tm[q + 1] - tm[p]), 0)
    # source notes sung between the partners count against the piece unless they are ornaments
    # (shorter than ORNAMENT beats once laid on the target) or repeat the note before them
    dur = al.fold * (source.off - source.on)
    same = np.r_[False, np.abs(np.diff(source.fpitch)) <= PITCH_TOL]
    cost = ((dur >= ORNAMENT) & ~same).astype(int)
    csum = np.r_[0, np.cumsum(cost)]
    mcost = np.r_[0, np.cumsum(cost[partner[m]])]
    extra = (csum[sb + 1] - csum[sa]) - (mcost[q + 1] - mcost[p])
    n_s = matched + np.maximum(extra, 0)
    span = np.maximum(n_t, n_s)
    beats = held_until(target)[b] - target.on[a]
    need = np.where(beats >= long_beats, min(long_rate, min_rate), min_rate)
    ok = (q >= p) & (matched >= min_piece) & (sb >= sa) & (matched >= need * span - 1e-9) & (beats >= min_beats - 1e-9)
    tr = tempo_ratio(source.ibi, ibi_target, al.fold)
    out = []
    for i, j in zip(*np.nonzero(ok)):
        ai, bi = int(a[i, j]), int(b[i, j])
        pc = Piece(al.work_id, ai, bi, int(sa[i, j]), int(sb[i, j]), al.fold, al.offset, al.transpose,
                   int(matched[i, j]), int(n_t[i, j]), int(n_s[i, j]), tr)
        pc.weight = piece_weight(pc.matched, max(pc.n_target, pc.n_source), **weights) if weights else float(pc.matched)
        pc.pairs = [(int(x), int(partner[x])) for x in m[i:j + 1]]
        out.append(pc)
    return out


# --------------------------------------------------------------------------- index
def contour_notes(seq: Seq) -> np.ndarray:
    """Indices of the notes that change pitch (a note repeating the one before it - another
    syllable on the same pitch - is skipped), so seeds survive re-sung or split notes."""
    if not len(seq):
        return np.zeros(0, dtype=int)
    return np.flatnonzero(np.r_[True, np.diff(seq.pitch) != 0])


def ngram_keys(seq: Seq, n: int = NGRAM) -> list[tuple[tuple | None, tuple[int, ...]]]:
    """(transposition- and tempo-invariant key, note indices) of the n-gram of pitch-changing
    notes (``contour_notes``) starting at each of them."""
    out = []
    idx = contour_notes(seq)
    on, pitch = seq.on, seq.pitch
    for k in range(len(idx) - n + 1):
        js = idx[k:k + n]
        ioi = np.diff(on[js])
        if np.any(ioi <= 1e-6):
            out.append((None, tuple(int(x) for x in js)))
            continue
        iv = tuple(int(x) for x in np.diff(pitch[js]))
        if RATIO_CLASS > 0:
            iv += tuple(int(np.clip(round(math.log2(ioi[q + 1] / ioi[q]) * RATIO_CLASS), -4, 4)) for q in range(n - 2))
        out.append((iv, tuple(int(x) for x in js)))
    return out


class Index:
    """n-gram -> [(song, its note indices)] over a corpus of sequences."""

    def __init__(self, seqs: dict[str, Seq], n: int = NGRAM):
        self.seqs = seqs
        self.n = n
        self.table: dict[tuple, list[tuple[str, tuple[int, ...]]]] = defaultdict(list)
        for wid, s in seqs.items():
            for key, js in ngram_keys(s, n):
                if key is not None:
                    self.table[key].append((wid, js))

    def seeds(self, target: Seq, exclude: set[str] = frozenset()
              ) -> Iterable[tuple[tuple[int, ...], str, tuple[int, ...]]]:
        """(target notes, source song, source notes) of every shared n-gram."""
        for key, ti in ngram_keys(target, self.n):
            if key is None:
                continue
            for wid, js in self.table.get(key, ()):
                if wid not in exclude:
                    yield ti, wid, js


def seed_alignment(target: Seq, source: Seq, ti, sj, ibi_target: float) -> Alignment | None:
    """The alignment a seed (matching target notes ``ti`` and source notes ``sj``) implies (None
    when no allowed fold fits its span, or the piece would be heard more than ``MAX_OCTAVE``
    octaves from the target)."""
    ti, sj = np.asarray(ti), np.asarray(sj)
    t_span = target.on[ti[-1]] - target.on[ti[0]]
    s_span = source.on[sj[-1]] - source.on[sj[0]]
    if t_span <= 0 or s_span <= 0:
        return None
    r = t_span / s_span
    folds = [f for f in allowed_folds(source.ibi, ibi_target) if abs(math.log2(r / f)) <= FOLD_TOL]
    if not folds:
        return None
    f = folds[0]
    off = float(np.median(target.on[ti] - f * source.on[sj]))
    tr = int(target.pitch[ti[0]] - source.pitch[sj[0]])
    if abs(tr) > MAX_SHIFT + 12 * MAX_OCTAVE:
        return None
    return Alignment(source.work_id, f, round(off * 16) / 16, tr)


def find_pieces(target: Seq, index: Index, *, ibi_target: float, exclude: set[str] = frozenset(),
                min_piece: int = MIN_PIECE, min_rate: float = MIN_RATE, min_beats: float = 0.0,
                long_beats: float = float("inf"), long_rate: float = LONG_RATE, **weights) -> list[Piece]:
    """Every valid piece of every seeded alignment of the corpus onto ``target``."""
    seen: set[Alignment] = set()
    done: set[tuple] = set()
    out: list[Piece] = []
    for ti, wid, sj in index.seeds(target, exclude):
        src = index.seqs[wid]
        al = seed_alignment(target, src, ti, sj, ibi_target)
        if al is None or al in seen:
            continue
        seen.add(al)
        partner = match_notes(target, src, al.fold, al.offset, al.transpose)
        off = refine_offset(target, src, al.fold, partner, al.offset)
        if abs(off - al.offset) > 1e-3:
            partner = match_notes(target, src, al.fold, off, al.transpose)
        key = (wid, al.fold, al.transpose, tuple(partner))
        if key in done:
            continue
        done.add(key)
        al2 = Alignment(wid, al.fold, round(off, 4), al.transpose)
        out += pieces_of(target, src, al2, partner, ibi_target=ibi_target, min_piece=min_piece,
                         min_rate=min_rate, min_beats=min_beats, long_beats=long_beats, long_rate=long_rate,
                         **weights)
    return out
