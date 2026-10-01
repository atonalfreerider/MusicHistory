"""The target loop and its cover: which pieces of other songs rebuild it.

**Loop** (``choose_loop``): whole bars of the target (its measured meter and downbeats,
``mashup.tracks``), ``LOOP_BARS`` long, at most ``MAX_LOOP_SECONDS``: the window whose bars are
sung most densely (stable notes per bar, vocal-active bars), whose chords run on from its last
bar into its first (the bar after the window agrees with its first bar, so the loop repeats
cleanly), that opens on the tonic chord where possible and lasts nearest ``IDEAL_SECONDS``.
Its notes (stability >= ``MIN_STABILITY``, onset in the window) are the target melody, in loop
beats.

**Cover** (``cover``): a weighted interval schedule over the loop's notes. Every candidate
piece (``match.Piece``, at least ``MIN_SPAN_BARS`` bar long) covers target notes ``a..b`` with
weight ``(matched - MISS_COST * misses) * (1 + LENGTH_BONUS * (matched - 1)) - PIECE_COST``
(longer pieces are worth more per note, a miss costs what a match earns, every piece costs
``PIECE_COST``: a 4-bar loop takes about 2-4 pieces), pieces may not share target notes, and
the schedule maximizing the summed weight is found by dynamic programming. A song
used for more than one piece costs ``DUP_COST`` per extra use: the schedule is re-solved with
a repeated song restricted to one of its spans until no variant does better.

Pure note arithmetic: no audio here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ..mashup.chain import bar_agreement
from ..mashup.tracks import Track
from .match import Piece, Seq
from .notes import SongNotes

LOOP_BARS = (4, 6, 8)
IDEAL_SECONDS = 16.0
MIN_LOOP_SECONDS = 6.0
MAX_LOOP_SECONDS = 22.0
MIN_STABILITY = 0.5
MIN_LOOP_NOTES = 14
MIN_NOTES_PER_BAR = 1.5
LENGTH_BONUS = 0.2
MISS_COST = 1.0
PIECE_COST = 6.0
MIN_SPAN_BARS = 1.0          # a piece spans at least this many bars of the target ...
LONG_SPAN_BARS = 2.0         # ... and from this many on it may match a little less (``match.LONG_RATE``)
RECOGNIZABLE_SECONDS = 3.5   # a piece this long (target span) is fully recognizable in the ranking
DUP_COST = 2.0
KEEP_PER_SPAN = 3             # candidate pieces kept per target span (distinct songs)


@dataclass
class Loop:
    work_id: str
    start_bar: int
    bars: int
    bpb: int
    beat0: int                        # index of the loop's first beat in the target's beat grid
    notes: Seq                        # the melody, in loop beats
    stability: np.ndarray             # per note
    score: float = 0.0
    seam: float | None = None         # chord agreement of the bar after the loop with its first bar
    params: dict = field(default_factory=dict)

    @property
    def beats(self) -> int:
        return self.bars * self.bpb

    def seconds(self, beat_times: np.ndarray) -> float:
        return float(beat_times[self.beat0 + self.beats] - beat_times[self.beat0])


def stable_seq(sn: SongNotes, min_stability: float = MIN_STABILITY) -> tuple[Seq, np.ndarray]:
    ok = sn.notes[:, 5] >= min_stability
    n = sn.notes[ok]
    return Seq(sn.work_id, n[:, 2], n[:, 3], n[:, 4].astype(int), sn.ibi, n[:, 6]), n[:, 5]


def choose_loop(track: Track, sn: SongNotes, *, bar_options=LOOP_BARS,
                accept: Callable[[Seq, np.ndarray], bool] | None = None) -> Loop | None:
    """The target's main phrase as a loop of whole bars (None when no window is sung enough;
    ``accept(notes, stability)`` may reject a window's notes, e.g. a chanted or spoken one)."""
    seq, stab = stable_seq(sn)
    bpb = track.bpb
    tonic = track.tonic * 2 + (1 if track.mode == "minor" else 0)
    best: Loop | None = None
    for L in bar_options:
        for s in range(0, track.n_bars - L + 1):
            b0 = track.bar_beat(s)
            b1 = b0 + L * bpb
            if b1 >= len(track.beats):
                continue
            secs = float(track.beats[b1] - track.beats[b0])
            if not MIN_LOOP_SECONDS <= secs <= MAX_LOOP_SECONDS:
                continue
            w = seq.window(b0, b1)
            sel = (seq.on >= b0) & (seq.on < b1)
            n = len(w)
            if n < MIN_LOOP_NOTES or n / L < MIN_NOTES_PER_BAR or (accept is not None and not accept(w, stab[sel])):
                continue
            sung = np.mean([track.bar_vocal(s + k) > 0 for k in range(L)])
            bars_with_notes = len({int(x // bpb) for x in w.on})
            seam = bar_agreement(track.bar_chords(s + L), track.bar_chords(s)) if s + L < track.n_bars else None
            first = track.bar_chords(s)
            opens_tonic = bool(first) and first[0] == tonic
            score = (0.6 * bars_with_notes / L + 0.4 * sung + 0.25 * min(1.0, n / (3.0 * L))
                     + 0.3 * (seam if seam is not None else 0.4) + 0.1 * opens_tonic
                     - 0.35 * abs(np.log2(secs / IDEAL_SECONDS)) + 0.05 * float(np.mean(stab[sel])))
            if best is None or score > best.score:
                best = Loop(sn.work_id, s, L, bpb, b0, w, stab[sel], score, seam,
                            {"seconds": round(secs, 3), "bars_with_notes": bars_with_notes, "sung_bars": float(sung),
                             "opens_tonic": opens_tonic})
    return best


# --------------------------------------------------------------------------- schedule
def prune(pieces: list[Piece], keep: int = KEEP_PER_SPAN) -> list[Piece]:
    """Per target span, the best piece of each song, then the ``keep`` best songs."""
    by_span: dict[tuple[int, int], dict[str, Piece]] = defaultdict(dict)
    for p in pieces:
        d = by_span[(p.a, p.b)]
        q = d.get(p.work_id)
        if q is None or (p.weight, p.rate) > (q.weight, q.rate):
            d[p.work_id] = p
    out = []
    for d in by_span.values():
        out += sorted(d.values(), key=lambda p: (-p.weight, -p.rate, p.work_id))[:keep]
    return out


def schedule(pieces: list[Piece], n_notes: int) -> tuple[list[Piece], float]:
    """Disjoint pieces (over target notes 0..n-1) with the largest summed weight."""
    opt = np.zeros(n_notes + 1)
    choice: list[Piece | None] = [None] * (n_notes + 1)
    ending: dict[int, list[Piece]] = defaultdict(list)
    for p in pieces:
        if p.weight > 0 and 0 <= p.a <= p.b < n_notes:
            ending[p.b].append(p)
    for x in range(1, n_notes + 1):
        opt[x] = opt[x - 1]
        choice[x] = None
        for p in ending.get(x - 1, ()):
            v = opt[p.a] + p.weight
            if v > opt[x] + 1e-12 or (abs(v - opt[x]) <= 1e-12 and choice[x] is not None
                                       and (p.b - p.a) > (choice[x].b - choice[x].a)):
                opt[x] = v
                choice[x] = p
    out = []
    x = n_notes
    while x > 0:
        p = choice[x]
        if p is None:
            x -= 1
        else:
            out.append(p)
            x = p.a
    return out[::-1], float(opt[n_notes])


def objective(chosen: list[Piece], dup_cost: float = DUP_COST) -> float:
    uses: dict[str, int] = defaultdict(int)
    for p in chosen:
        uses[p.work_id] += 1
    return sum(p.weight for p in chosen) - dup_cost * sum(u - 1 for u in uses.values())


def cover(pieces: list[Piece], n_notes: int, *, dup_cost: float = DUP_COST, rounds: int = 8) -> list[Piece]:
    """The best schedule counting ``dup_cost`` per repeated song."""
    pool = list(pieces)
    chosen, _ = schedule(pool, n_notes)
    best_val = objective(chosen, dup_cost)
    for _ in range(rounds):
        uses: dict[str, list[Piece]] = defaultdict(list)
        for p in chosen:
            uses[p.work_id].append(p)
        dups = {w: ps for w, ps in uses.items() if len(ps) > 1}
        if not dups:
            break
        improved = False
        for w, ps in dups.items():
            for keep in ps:
                trial = [p for p in pool if p.work_id != w or (p.a <= keep.b and p.b >= keep.a)]
                c, _ = schedule(trial, n_notes)
                v = objective(c, dup_cost)
                if v > best_val + 1e-9:
                    best_val, chosen, pool, improved = v, c, trial, True
            if improved:
                break
        if not improved:
            break
    return chosen


@dataclass
class Mosaic:
    """A cover's summary numbers."""

    pieces: list[Piece]
    n_notes: int
    piece_seconds: list[float] = field(default_factory=list)    # each piece's target span (s)
    bars: int = 4

    @property
    def mean_seconds(self) -> float:
        return float(np.mean(self.piece_seconds)) if self.piece_seconds else 0.0

    @property
    def matched(self) -> int:
        return sum(p.matched for p in self.pieces)

    @property
    def match(self) -> float:
        return self.matched / max(self.n_notes, 1)

    @property
    def coverage(self) -> float:
        return sum(p.b - p.a + 1 for p in self.pieces) / max(self.n_notes, 1)

    @property
    def songs(self) -> int:
        return len({p.work_id for p in self.pieces})

    @property
    def mean_notes(self) -> float:
        return float(np.mean([p.matched for p in self.pieces])) if self.pieces else 0.0

    @property
    def mean_rate(self) -> float:
        return float(np.mean([p.rate for p in self.pieces])) if self.pieces else 0.0

    def quality(self) -> float:
        """0..~1: note-for-note match and recognizable (long) pieces first - mean piece seconds,
        notes per piece, few pieces (more than one a bar costs) - then coverage and distinct songs."""
        if not self.pieces:
            return 0.0
        k = len(self.pieces)
        return (0.3 * self.match + 0.1 * self.coverage + 0.1 * self.mean_rate
                + 0.3 * min(1.0, self.mean_seconds / RECOGNIZABLE_SECONDS) + 0.2 * min(1.0, self.mean_notes / 10.0)
                - 0.05 * max(0, k - self.bars) - 0.05 * (k - self.songs) - (0.15 if self.songs < 2 else 0.0))
