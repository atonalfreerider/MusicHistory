"""Harmony voices: other songs' melody spans that sound against the target loop in consonance.

A voice is one continuous span of another song's sung notes laid over the whole loop by one
alignment (fold ``f`` with an allowed tempo ratio, a start on a source beat, an audio
transposition of -6..6 semitones). Both melodies are sampled on a grid of ``CELL`` beats of the
loop (the source at its own 1/8-beat resolution, read every ``2/f`` cells), and wherever both
sing the **interval** (harmony minus target, in semitones) scores by its class mod 12
(``CONSONANCE``): thirds and sixths 1.0, fifths and fourths 0.8, the octave or unison 0.3 (a
doubling, not a harmony), seconds and sevenths 0.2, the minor second, major seventh and tritone
0.

* ``consonance``: the mean interval score over the cells where both sing;
* ``coverage``: the share of the target's sung cells the voice sings with;
* ``strong``: the share of the target's notes starting on a strong beat (the bar's first beat
  or its middle) that meet a dissonance (score < 0.3);
* ``doubling``: the share of shared cells at the octave or unison;
* ``chord_fit``: the share of the voice's own cells (also where the target rests) whose pitch
  class is a tone of the loop's chord there;
* ``spread``: the mean distance (semitones) between the two voices.

``score = coverage * consonance - STRONG_W * strong - DOUBLE_W * max(0, doubling - DOUBLE_FREE)
+ CHORD_W * chord_fit - SPREAD_W * max(0, spread - 12) / 12``; a voice needs ``MIN_COVERAGE``.
A second voice also scores against the first (consonant, not doubling it) and comes from
another song.

Only pitches and times: no words.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..mashup.chain import triad
from .match import Seq, allowed_folds, tempo_ratio

CELL = 0.25                  # loop beats per cell
SRC_RES = 8                  # source cells per source beat
CONSONANCE = np.array([0.3, 0.0, 0.2, 1.0, 1.0, 0.8, 0.0, 0.8, 1.0, 1.0, 0.2, 0.0])
DISSONANT = 0.3
SHIFTS = tuple(range(-6, 7))
MIN_COVERAGE = 0.5
STRONG_W = 0.5
DOUBLE_W = 1.0
DOUBLE_FREE = 0.2
CHORD_W = 0.25
SPREAD_W = 0.3
PAIR_W = 0.3                 # second voice: consonance with the first
PAIR_DOUBLE_W = 0.5


def roll(seq: Seq, n_cells: int, res: float, start: float = 0.0) -> np.ndarray:
    """Pitch sounding at the centre of every cell (``res`` beats wide from ``start``), -1 silent."""
    out = np.full(n_cells, -1, dtype=int)
    centres = start + (np.arange(n_cells) + 0.5) * res
    for on, off, p in zip(seq.on, seq.off, seq.pitch):
        a = int(np.searchsorted(centres, on, side="left"))
        b = int(np.searchsorted(centres, off, side="left"))
        out[a:b] = p
    return out


def source_roll(seq: Seq) -> np.ndarray:
    """A whole source song at ``1/SRC_RES`` beat resolution from its beat 0."""
    if not len(seq):
        return np.full(0, -1, dtype=int)
    n = int(np.ceil(max(float(seq.off.max()), 1.0) * SRC_RES)) + 1
    return roll(seq, n, 1.0 / SRC_RES)


@dataclass
class LoopContext:
    """The target loop on the cell grid."""

    pitch: np.ndarray                  # target pitch per cell (-1 rests)
    strong: np.ndarray                 # cells where a target note starts on a strong beat
    chord: np.ndarray                  # (n_cells, 12) bool: tones of the loop's chord per cell
    beats: int
    ibi: float

    @property
    def n_cells(self) -> int:
        return len(self.pitch)


def context(target: Seq, loop_beats: int, bpb: int, slot_chords: list[list[int]], ibi: float) -> LoopContext:
    """``slot_chords[k]``: the chord labels of loop bar k's half-bar slots (-1 silent)."""
    n = int(round(loop_beats / CELL))
    tp = roll(target, n, CELL)
    strong = np.zeros(n, dtype=bool)
    strong_beats = {0, bpb // 2} if bpb % 2 == 0 and bpb >= 4 else {0}
    for on in target.on:
        q = on / 1.0
        nearest = round(q)
        if abs(q - nearest) <= 0.125 and (nearest % bpb) in strong_beats:
            c = int(np.clip(round(on / CELL), 0, n - 1))
            strong[c] = True
    chord = np.zeros((n, 12), dtype=bool)
    cells_per_bar = int(round(bpb / CELL))
    for k, labs in enumerate(slot_chords):
        if not labs:
            continue
        per = cells_per_bar / len(labs)
        for i, lab in enumerate(labs):
            if lab < 0:
                continue
            a = int(round(k * cells_per_bar + i * per))
            b = int(round(k * cells_per_bar + (i + 1) * per))
            chord[a:b, list(triad(lab))] = True
    return LoopContext(tp, strong, chord, loop_beats, ibi)


@dataclass
class Voice:
    work_id: str
    fold: float
    start: float                       # source beat heard at loop beat 0
    shift: int
    tempo_ratio: float
    score: float
    consonance: float
    coverage: float
    strong: float
    doubling: float
    chord_fit: float
    spread: float
    cells: np.ndarray = field(repr=False, default=None)    # heard pitch per loop cell (-1 silent)

    def stats(self) -> dict:
        return {k: round(float(getattr(self, k)), 4) for k in
                ("score", "consonance", "coverage", "strong", "doubling", "chord_fit", "spread")}


def windows(sroll: np.ndarray, fold: float, n_cells: int) -> tuple[np.ndarray, np.ndarray]:
    """(source start beats, (n_starts, n_cells) source pitch under every loop cell)."""
    step = SRC_RES * CELL / fold
    offs = (np.arange(n_cells) + 0.5) * step
    span = int(np.ceil(offs[-1])) + 1
    starts = np.arange(0, max(0, len(sroll) - span) // SRC_RES + 1)
    if len(sroll) < span or not len(starts):
        return np.zeros(0), np.zeros((0, n_cells), dtype=int)
    idx = starts[:, None] * SRC_RES + np.floor(offs)[None, :].astype(int)
    return starts.astype(float), sroll[np.clip(idx, 0, len(sroll) - 1)]


def evaluate(ctx: LoopContext, W: np.ndarray, others: list[np.ndarray] = ()) -> dict[str, np.ndarray]:
    """Scores of every (start, shift) for source windows ``W`` (n_starts, n_cells)."""
    shifts = np.asarray(SHIFTS)[None, :, None]
    tp = ctx.pitch[None, None, :]
    H = np.where((W >= 0)[:, None, :], W[:, None, :] + shifts, -1)  # (n_starts, n_shifts, n_cells)
    sing = H >= 0
    t_on = tp >= 0
    both = sing & t_on
    D = H - tp
    cons = CONSONANCE[np.mod(D, 12)]
    n_both = both.sum(axis=2)
    n_t = max(int(t_on.sum()), 1)
    coverage = n_both / n_t
    safe = np.maximum(n_both, 1)
    consonance = np.where(both, cons, 0).sum(axis=2) / safe
    doubling = np.where(both, np.mod(D, 12) == 0, False).sum(axis=2) / safe
    spread = np.where(both, np.abs(D), 0).sum(axis=2) / safe
    st = ctx.strong[None, None, :]
    n_strong = max(int(ctx.strong.sum()), 1)
    strong = (both & st & (cons < DISSONANT)).sum(axis=2) / n_strong
    pcs = np.mod(np.where(sing, H, 0), 12)
    known = ctx.chord.any(axis=1)[None, None, :]
    fit = np.take_along_axis(np.broadcast_to(ctx.chord[None, None], (*H.shape, 12)), pcs[..., None], axis=3)[..., 0]
    n_sing_known = np.maximum((sing & known).sum(axis=2), 1)
    chord_fit = (fit & sing & known).sum(axis=2) / n_sing_known
    score = (coverage * consonance - STRONG_W * strong - DOUBLE_W * np.maximum(0, doubling - DOUBLE_FREE)
             + CHORD_W * chord_fit - SPREAD_W * np.maximum(0, spread - 12) / 12)
    for o in others:
        ob = sing & (o >= 0)[None, None, :]
        Do = H - o[None, None, :]
        n_ob = np.maximum(ob.sum(axis=2), 1)
        c_o = np.where(ob, CONSONANCE[np.mod(Do, 12)], 0).sum(axis=2) / n_ob
        d_o = np.where(ob, np.mod(Do, 12) == 0, False).sum(axis=2) / n_ob
        has = ob.sum(axis=2) > 0
        score = score + np.where(has, PAIR_W * (c_o - 0.5) - PAIR_DOUBLE_W * d_o, 0.0)
    score = np.where(coverage >= MIN_COVERAGE, score, -np.inf)
    return {"score": score, "consonance": consonance, "coverage": coverage, "strong": strong, "doubling": doubling,
            "chord_fit": chord_fit, "spread": spread, "H": H}


def search(ctx: LoopContext, rolls: dict[str, tuple[np.ndarray, float]], *, exclude: set[str] = frozenset(),
           others: list[np.ndarray] = (), top: int = 1) -> list[Voice]:
    """The best voices over every song in ``rolls`` ({work_id: (source roll, ibi)}), one per song."""
    best: list[Voice] = []
    for wid, (sroll, ibi) in rolls.items():
        if wid in exclude:
            continue
        song_best: Voice | None = None
        for f in allowed_folds(ibi, ctx.ibi):
            starts, W = windows(sroll, f, ctx.n_cells)
            if not len(starts):
                continue
            ev = evaluate(ctx, W, others)
            k = int(np.argmax(ev["score"]))
            i, j = divmod(k, len(SHIFTS))
            s = float(ev["score"][i, j])
            if not np.isfinite(s) or (song_best is not None and s <= song_best.score):
                continue
            song_best = Voice(wid, f, float(starts[i]), int(SHIFTS[j]), tempo_ratio(ibi, ctx.ibi, f), s,
                              float(ev["consonance"][i, j]), float(ev["coverage"][i, j]), float(ev["strong"][i, j]),
                              float(ev["doubling"][i, j]), float(ev["chord_fit"][i, j]), float(ev["spread"][i, j]),
                              ev["H"][i, j].copy())
        if song_best is not None:
            best.append(song_best)
    best.sort(key=lambda v: (-v.score, v.work_id))
    return best[:top]


def voices(ctx: LoopContext, rolls: dict[str, tuple[np.ndarray, float]], *, exclude: set[str] = frozenset(),
           n: int = 2, min_score: float = 0.35) -> list[Voice]:
    """One or two harmony voices from different songs (the second only when it scores
    ``min_score`` with the first)."""
    first = search(ctx, rolls, exclude=exclude)
    if not first:
        return []
    out = [first[0]]
    while len(out) < n:
        nxt = search(ctx, rolls, exclude=set(exclude) | {v.work_id for v in out}, others=[v.cells for v in out])
        if not nxt or nxt[0].score < min_score:
            break
        out.append(nxt[0])
    return out


def heard_notes(source: Seq, v: Voice, loop_beats: int) -> list[list[float]]:
    """The voice's notes as heard: [start, end, pitch] in loop beats (clipped to the loop)."""
    out = []
    for on, off, p in zip(source.on, source.off, source.pitch):
        a, b = (on - v.start) * v.fold, (off - v.start) * v.fold
        if b <= 0 or a >= loop_beats:
            continue
        a, b = max(a, 0.0), min(b, float(loop_beats))
        if b - a > 1e-3:
            out.append([float(a), float(b), int(p + v.shift)])
    return out


def consonance_of(target: Seq, voice_notes: list[list[float]], loop_beats: int) -> float | None:
    """Mean interval score of a (heard) voice against the target, on the cell grid."""
    n = int(round(loop_beats / CELL))
    tp = roll(target, n, CELL)
    vs = Seq("v", [x[0] for x in voice_notes], [x[1] for x in voice_notes], [int(round(x[2])) for x in voice_notes])
    vp = roll(vs, n, CELL)
    both = (tp >= 0) & (vp >= 0)
    if not both.any():
        return None
    return float(np.mean(CONSONANCE[np.mod(vp[both] - tp[both], 12)]))
