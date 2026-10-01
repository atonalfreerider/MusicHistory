"""Chain planner: which bar of which song (and which stem) sounds in every bar of the mix.

For a path S1..Sn (``Track``s, already regridded to neighbouring tempo octaves):

1. **full**: S1 (counted as measured or in the tempo octave nearest S2's, whichever plan
   matches better) from its start bar (the first of bars 0..3 that opens on its tonic chord) for
   about ``full_bars`` bars (``full_bars`` - 2 .. + 2, chosen with changeover 1);
2. **changeover** i: S_i's instrumental continues natively (whole bars, repeating a loop of
   ``period`` bars when the preview runs out; or restarting at another bar of S_i whose
   preceding bar has the chords of the bar just played, so the harmony runs on) under S_{i+1}'s vocal window: ``L`` whole bars
   of S_{i+1} starting at bar ``b0``, transposed by ``shift`` semitones. ``L`` is
   ``co_seconds`` rounded to S_i's bars. The window, the shift and (after S1) up to
   ``max_extra`` extra full bars of S_i before it are chosen to maximize the bar-level chord
   agreement between S_{i+1}'s own chords under its vocal and S_i's instrumental chords, with
   the key-derived shift preferred (relative normalization: C major = A minor). When the best
   agreement stays under ``min_match`` the changeover is shortened bar by bar (down to
   ``min_bars``) until it reaches it; if it never does, the best window is kept and flagged;
3. **morph**: S_{i+1} in full for ``morph_bars`` bars (the bars right after its vocal window, so
   its vocal lands on its own backing), gliding from S_i's key and tempo to its own;
4. then S_{i+1}'s instrumental carries changeover i+1, ... and the last song plays
   ``final_bars`` more bars in full (as many as its preview has, at least 4).

Chord agreement per half-bar chord slot (``Track.bar_chords``): 1 for the same chord, 0.5 for
triads sharing two tones (C~Cm, C~Am, C~Em), 0 otherwise; silent slots do not count; a bar's
agreement is the mean over its slots and a window's (``chord_match``) the mean over its bars.
When the bars right after a vocal window run past the end of the preview, the morph repeats
bars of the song's loop (``LOOP_MORPH_PENALTY`` per repeated bar).

A song may also enter with its bar lines half a bar off the previous song's (``half_bar``;
``HALF_BAR_PENALTY``): a progression rotated by half a bar (C G | Am F against G Am | F C)
matches only that way. Each song after the first is counted in the tempo octave (regrid x1, x2 or x0.5, tempo ratio
within ``TEMPO_RANGE``) that gives the best changeover, ``TEMPO_WEIGHT`` per octave of tempo
change counting against it.

A song without a sung vocal (``lead == 'other'``) leads its changeover with its ``other`` stem
(melodic instruments) over the previous song's rhythm section (drums + bass).

Pure bar arithmetic: no audio here (see ``timeline.py`` for seconds).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..paths.plan import wrap
from .tracks import fold_factor

SHIFT_PENALTY = 0.12          # objective cost of a transposition other than the key-derived one
COVER_WEIGHT = 0.1
PHRASE_BONUS = 0.03
EXTRA_PENALTY = 0.015         # per extra full bar before a changeover
MIN_COVER = 0.5
TEMPO_RANGE = (0.66, 1.5)     # tempo ratios (instrumental / vocal source) a changeover may use
LOOP_MORPH_PENALTY = 0.04    # per morph bar that repeats a bar of the loop
HALF_BAR_PENALTY = 0.06      # a vocal window whose bar lines fall on the instrumental's mid-bar
JUMP_PENALTY = 0.03          # the backing restarts elsewhere in its song (at a matching bar line)
JUMP_MIN_AGREE = 0.75         # ... only where the bar before the target agrees with the bar just played
TEMPO_WEIGHT = 0.3            # objective cost per octave of tempo change


@dataclass(frozen=True)
class OutBar:
    kind: str                         # 'full' | 'changeover' | 'morph'
    inst: tuple[int, int]             # (song index, source bar) of the backing
    vocal: tuple[int, int] | None     # (song index, source bar) of the lead; full/morph: == inst


@dataclass
class Hop:
    a: int
    b: int
    out_start: int                    # first changeover bar (index into ChainPlan.bars)
    bars: int                         # changeover length L
    b0: int                           # first source bar of b's vocal window
    a_bars: list[int]                 # a's source bars under the window
    shift: int                        # semitones applied to b's vocal
    key_shift: int                    # the key-derived shift
    chord_match: float
    per_bar: list[float | None]
    cover: float                      # vocal-active share of the window
    extra: int                        # full bars of a before the changeover (after its morph)
    target_bars: int                  # L before shortening
    ok: bool                          # chord_match >= min_match
    lead: str                         # b's lead stem
    jump: bool = False                # a's backing restarts at another bar of its song


@dataclass
class ChainPlan:
    bars: list[OutBar]
    hops: list[Hop]
    start_bar: int
    periods: list[int]
    phrase_bars: int
    tracks: list = field(default_factory=list)      # the songs as planned (regridded)
    params: dict = field(default_factory=dict)

    def segments(self) -> list[tuple[str, int, int, int, int | None]]:
        """(kind, first bar, end bar (exclusive), instrumental song, vocal song) runs."""
        out: list[list] = []
        for i, b in enumerate(self.bars):
            key = (b.kind, b.inst[0], None if b.vocal is None else b.vocal[0])
            if out and (out[-1][0], out[-1][3], out[-1][4]) == key and out[-1][2] == i:
                out[-1][2] = i + 1
            else:
                out.append([key[0], i, i + 1, key[1], key[2]])
        return [tuple(s) for s in out]


# --------------------------------------------------------------------------- chords
def shift_label(label: int, semitones: int) -> int:
    if label < 0:
        return label
    return ((label // 2 + semitones) % 12) * 2 + label % 2


def triad(label: int) -> frozenset[int]:
    r, minor = divmod(label, 2)
    return frozenset({r, (r + (3 if minor else 4)) % 12, (r + 7) % 12})


def beat_agreement(a: int, b: int) -> float | None:
    """1 for the same chord, 0.5 for triads sharing two tones (same root with the other
    quality, relative or mediant: C~Cm, C~Am, C~Em), 0 otherwise; None when either is silent."""
    if a < 0 or b < 0:
        return None
    if a == b:
        return 1.0
    return 0.5 if len(triad(a) & triad(b)) == 2 else 0.0


def bar_agreement(a: list[int], b: list[int], shift: int = 0) -> float | None:
    """Mean beat agreement of two bars (``b`` transposed by ``shift``); bars of different
    lengths are compared at proportional positions."""
    if not a or not b:
        return None
    n = max(len(a), len(b))
    vals = []
    for k in range(n):
        x = a[min(len(a) - 1, k * len(a) // n)]
        y = shift_label(b[min(len(b) - 1, k * len(b) // n)], shift)
        v = beat_agreement(x, y)
        if v is not None:
            vals.append(v)
    return float(np.mean(vals)) if vals else None


def window_agreement(a_bars: list[list[int]], b_bars: list[list[int]], shift: int = 0
                     ) -> tuple[float, list[float | None]]:
    per = [bar_agreement(x, y, shift) for x, y in zip(a_bars, b_bars)]
    vals = [v for v in per if v is not None]
    return (float(np.mean(vals)) if vals else 0.0), per


# --------------------------------------------------------------------------- loops
def loop_period(bar_chords: list[list[int]], phrase_bars: int = 8, candidates=(2, 4, 6, 8, 12, 16)) -> int:
    """Bars to jump back when a song's preview runs out: the candidate whose bars repeat best
    (bar b vs bar b - J), preferring multiples of 4 and of the phrase."""
    n = len(bar_chords)
    best, best_score = None, -1.0
    for J in candidates:
        if J > n - 1:
            continue
        score, _ = window_agreement(bar_chords[:n - J], bar_chords[J:])
        score += (0.04 if J % 4 == 0 else 0.0) + (0.08 if J % phrase_bars == 0 else 0.0) + 0.002 * J
        if score > best_score:
            best, best_score = J, score
    if best is None:
        return max(1, n)
    return best


def bar_sequence(start: int, length: int, n_bars: int, period: int) -> list[int]:
    """Source bars start, start+1, ...; a bar past the end is replaced by the one ``period``
    bars earlier (repeatedly), i.e. the last ``period`` bars loop."""
    period = max(1, min(period, n_bars))
    out = []
    for k in range(max(0, length)):
        b = start + k
        while b >= n_bars:
            b -= period
        out.append(max(0, b))
    return out


def choose_start(track, max_bar: int = 3, min_left: int = 8) -> int:
    """First of bars 0..max_bar opening on the tonic chord, leaving ``min_left`` bars; else 0."""
    tonic = track.tonic * 2 + (1 if track.mode == "minor" else 0)
    last = max(0, min(max_bar, track.n_bars - min_left))
    for b in range(last + 1):
        ch = track.bar_chords(b)
        if ch and ch[0] == tonic:
            return b
    for b in range(last + 1):                          # same root, either quality
        ch = track.bar_chords(b)
        if ch and ch[0] >= 0 and ch[0] // 2 == track.tonic:
            return b
    return 0


# --------------------------------------------------------------------------- hop search
@dataclass
class _Cand:
    obj: float
    match: float
    per: list
    L: int
    d: int
    b0: int
    shift: int
    cover: float
    a_bars: list[int]
    jump: bool = False


def agreement_matrix(A, B) -> np.ndarray:
    """M[s, a, b] = bar agreement of A's bar a with B's bar b transposed by s (NaN: silent)."""
    M = np.full((12, A.n_bars, B.n_bars), np.nan)
    a_ch = [A.bar_chords(x) for x in range(A.n_bars)]
    b_ch = [B.bar_chords(y) for y in range(B.n_bars)]
    for s in range(12):
        for x, ca in enumerate(a_ch):
            for y, cb in enumerate(b_ch):
                v = bar_agreement(ca, cb, s)
                if v is not None:
                    M[s, x, y] = v
    return M


def search_hop(A, B, *, cur_bar: int, last_bar: int | None, d_options, L0: int, min_bars: int, morph_bars: int,
               min_match: float, period_a: int) -> tuple[_Cand | None, int]:
    """Best changeover A -> B: extra full bars ``d`` of A (from ``cur_bar``), where A's backing
    starts (the next bar, or a jump to a bar whose predecessor agrees with the bar just
    played, ``JUMP_PENALTY``), the length ``L`` (from ``L0`` down), B's window ``b0`` and the
    shift (see the module docstring)."""
    key_shift = wrap(A.frame_shift - B.frame_shift)
    nA, nB = A.n_bars, B.n_bars
    if nA == 0 or nB == 0:
        return None, key_shift
    M = agreement_matrix(A, B)
    shifts = np.array([wrap(s) for s in range(12)])
    shift_cost = np.where(shifts == key_shift, 0.0, SHIFT_PENALTY)[:, None]
    a_self = [A.bar_chords(x) for x in range(nA)]
    overall: _Cand | None = None
    for L in range(L0, min_bars - 1, -1):
        nb0 = nB - L + 1
        if nb0 <= 0:
            continue
        b0s = np.arange(nb0)
        cols = b0s[:, None] + np.arange(L)[None, :]
        if B.lead == "vocals":
            voc = np.array([B.bar_vocal(y) for y in range(nB)])
            cover = voc[cols].mean(axis=1)
            prev = np.r_[0.0, voc[:-1]][:nb0]
            starts = (voc[:nb0] >= 0.5) & ((b0s == 0) | (prev < 0.25))
            valid = cover >= MIN_COVER
        else:
            cover = np.ones(nb0)
            starts = np.zeros(nb0, dtype=bool)
            valid = np.ones(nb0, dtype=bool)
        if not valid.any():
            continue
        looped = np.maximum(0, b0s + L + morph_bars - nB)
        bonus = COVER_WEIGHT * cover + PHRASE_BONUS * starts - LOOP_MORPH_PENALTY * looped
        best: _Cand | None = None
        for d in d_options:
            pre = bar_sequence(cur_bar, d, nA, period_a) if d > 0 else []
            prev_bar = pre[-1] if pre else last_bar
            natural = bar_sequence(cur_bar, d + 1, nA, period_a)[-1]
            entries = [(natural, 0.0, False)]
            if prev_bar is not None:
                for a_s in range(1, nA):
                    if a_s == natural:
                        continue
                    v = bar_agreement(a_self[a_s - 1], a_self[prev_bar])
                    if v is not None and v >= JUMP_MIN_AGREE:
                        entries.append((a_s, JUMP_PENALTY, True))
            for a_s, jp, jumped in entries:
                seq = bar_sequence(a_s, L, nA, period_a)
                vals = M[:, np.asarray(seq)[None, :], cols]               # (12, nb0, L)
                cnt = np.sum(np.isfinite(vals), axis=2)
                match = np.where(cnt > 0, np.nansum(vals, axis=2) / np.maximum(cnt, 1), 0.0)
                obj = match - shift_cost + bonus[None, :] - EXTRA_PENALTY * abs(d) - jp
                obj[:, ~valid] = -np.inf
                si, bi = np.unravel_index(int(np.argmax(obj)), obj.shape)
                if not np.isfinite(obj[si, bi]):
                    continue
                if best is None or obj[si, bi] > best.obj:
                    per = [None if not np.isfinite(v) else float(v) for v in vals[si, bi]]
                    best = _Cand(float(obj[si, bi]), float(match[si, bi]), per, L, d, int(bi), int(shifts[si]),
                                 float(cover[bi]), seq, jumped)
        if best is None:
            continue
        if best.match >= min_match:
            return best, key_shift
        if overall is None or best.obj + 0.01 * best.L > overall.obj + 0.01 * overall.L:
            overall = best
    return overall, key_shift


def _rank(c: _Cand, min_match: float) -> tuple:
    """Matching windows first, the longest of them; otherwise the best objective."""
    ok = c.match >= min_match
    return (ok, c.L if ok else 0, c.obj)


# --------------------------------------------------------------------------- plan
def plan_score(plan: ChainPlan) -> float:
    """Matched changeover bars, then mean chord agreement (to compare alternative plans)."""
    return sum(h.bars for h in plan.hops if h.ok) + sum(h.chord_match for h in plan.hops)


def plan_chain(tracks: list, **kw) -> ChainPlan:
    """The better plan of S1 counted as measured or in the tempo octave nearest S2's
    (``plan_score``); see ``plan_chain_with``."""
    if len(tracks) < 2:
        raise ValueError("a chain needs at least two songs")
    plans = []
    for f in dict.fromkeys((1.0, fold_factor(tracks[0].bpm, tracks[1].bpm))):
        first = tracks[0].regrid(f)
        if first.n_bars >= 4:
            plans.append(plan_chain_with([first, *tracks[1:]], **kw))
    if not plans:
        raise ValueError(f"song 1 has only {tracks[0].n_bars} bars")
    return max(plans, key=plan_score)


def plan_chain_with(tracks: list, *, co_seconds: float = 20.0, morph_bars: int = 2, full_bars: int = 8,
               final_bars: int = 8, phrase_bars: int = 8, min_bars: int = 4, min_match: float = 0.75,
               max_extra: int = 3, half_bar: bool = True) -> ChainPlan:
    if len(tracks) < 2:
        raise ValueError("a chain needs at least two songs")
    tracks = list(tracks)
    periods = [loop_period([t.bar_chords(b) for b in range(t.n_bars)], phrase_bars) for t in tracks]
    s1 = tracks[0]
    start = choose_start(s1, min_left=min(s1.n_bars, full_bars))
    bars: list[OutBar] = []
    hops: list[Hop] = []
    cur_bar = start
    last_bar: int | None = None
    for i in range(len(tracks) - 1):
        A, B = tracks[i], tracks[i + 1]
        L0 = max(min_bars, int(round(co_seconds / A.bar_seconds)))
        if i == 0:
            nominal = max(min_bars + 2, full_bars)
            d_options = list(range(nominal - 2, nominal + 3))
        else:
            d_options = list(range(0, max_extra + 1))
        cand, key_shift, B = None, 0, tracks[i + 1]
        variants = []
        for f in (1.0, 2.0, 0.5):
            Bf = tracks[i + 1].regrid(f)
            ratio = A.bpm / Bf.bpm
            if not (TEMPO_RANGE[0] <= ratio <= TEMPO_RANGE[1]):
                continue
            variants.append((Bf, TEMPO_WEIGHT * abs(np.log2(ratio))))
            if Bf.bpb % 2 == 0 and half_bar:
                variants.append((Bf.shift_phase(Bf.bpb // 2), TEMPO_WEIGHT * abs(np.log2(ratio)) + HALF_BAR_PENALTY))
        for Bv, cost in variants:
            if Bv.n_bars < min_bars:
                continue
            c, ks = search_hop(A, Bv, cur_bar=cur_bar, last_bar=last_bar, d_options=d_options, L0=L0,
                               min_bars=min_bars, morph_bars=morph_bars, min_match=min_match, period_a=periods[i])
            if c is None:
                continue
            c.obj -= cost
            if cand is None or _rank(c, min_match) > _rank(cand, min_match):
                cand, key_shift, B = c, ks, Bv
        if cand is not None and B is not tracks[i + 1]:
            tracks[i + 1] = B
            periods[i + 1] = loop_period([B.bar_chords(b) for b in range(B.n_bars)], phrase_bars)
        if cand is None:
            raise ValueError(f"no changeover window from song {i + 1} to song {i + 2} "
                             f"(song {i + 2} has {B.n_bars} bars, vocal share {B.vocal_share:.2f})")
        # bars of A before the changeover
        pre = cand.d
        for b in bar_sequence(cur_bar, pre, A.n_bars, periods[i]):
            bars.append(OutBar("full", (i, b), (i, b)))
        out_start = len(bars)
        for a_b, k in zip(cand.a_bars, range(cand.L)):
            bars.append(OutBar("changeover", (i, a_b), (i + 1, cand.b0 + k)))
        for b in bar_sequence(cand.b0 + cand.L, morph_bars, B.n_bars, periods[i + 1]):
            bars.append(OutBar("morph", (i + 1, b), (i + 1, b)))
        hops.append(Hop(a=i, b=i + 1, out_start=out_start, bars=cand.L, b0=cand.b0, a_bars=list(cand.a_bars),
                        shift=cand.shift, key_shift=key_shift, chord_match=round(cand.match, 4), per_bar=cand.per,
                        cover=round(cand.cover, 3), extra=pre if i else 0, target_bars=L0,
                        ok=cand.match >= min_match, lead=B.lead, jump=cand.jump))
        last_bar = bars[-1].inst[1]
        cur_bar = cand.b0 + cand.L + morph_bars
    last = tracks[-1]
    n_final = max(min_bars, min(final_bars, last.n_bars - cur_bar))
    for b in bar_sequence(cur_bar, n_final, last.n_bars, periods[-1]):
        bars.append(OutBar("full", (len(tracks) - 1, b), (len(tracks) - 1, b)))
    return ChainPlan(bars=bars, hops=hops, start_bar=start, periods=periods, phrase_bars=phrase_bars, tracks=tracks,
                     params=dict(co_seconds=co_seconds, morph_bars=morph_bars, full_bars=full_bars,
                                 final_bars=final_bars, min_bars=min_bars, min_match=min_match))
