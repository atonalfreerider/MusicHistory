"""Duet-loop planner (DESIGN §16): which bar of which song's vocal, and which instrumental,
sounds in every bar of a circular mix in the root song's key and tempo.

For a path S0..S(n-1) (``Track``s; n >= 3) with ``P`` bars per pair (``pair_seconds`` rounded
to whole bars of S0) and ``H`` handoff bars:

* **Cycle.** Pair k = the vocals of S_k and S_(k+1 mod n), k = 0..n-1; the loop has
  ``N = n*P`` bars and wraps from the last pair (S(n-1) + S0) to pair 0 (S0 + S1).
* **Layout** (mix bars; the file starts with pair 0 in full): pair k's **duet** run is
  ``[k*P, (k+1)*P - H)``, followed by the **handoff** into pair k+1, ``[(k+1)*P - H, (k+1)*P)``
  (the last one, into pair 0, ends the file). The pair boundary sits at the handoff's centre,
  so pair k nominally spans ``[k*P - H/2, (k+1)*P - H/2)``.
* **Voices.** S_j's vocal sounds through pairs j-1 and j as one window of ``2P + H`` bars of
  its own (consecutive; when the preview runs out, a loop of 4-16 of its bars repeats - the
  loop is chosen with the window, anywhere in the preview, ``loop_sequence``), mix
  bars ``[(j-1)*P - H, (j+1)*P)`` (circular): it fades in over the handoff into pair j-1
  (entering), carries on through the handoff into pair j and fades out over the handoff into
  pair j+1 (leaving). Its half-gain points therefore bound exactly its two pairs, and two
  vocals sound everywhere except inside handoffs, where the leaving and the entering vocal
  overlap (three, crossfading with equal power).
* **Matching.** S0 is counted as measured (or, when that is slower than ``SLOW_ROOT_BPM``,
  also in the tempo octave nearest the other songs'; the better plan wins). Every song but S0 is counted in the tempo octave nearest S0's (``fold_factor``;
  optionally with its bar lines half a bar off, ``HALF_BAR_PENALTY``) and transposed by the
  shift (key-derived preferred, ``SHIFT_PENALTY`` otherwise; S0: 0) and window start that
  maximize the bar-level chord agreement (``chain.bar_agreement``) between its own chords and the
  bed's under the window, plus small bonuses for vocal cover, a window opening a sung phrase
  and the share of distinct bars (less looping).
* **Bed.** S0's instrumental bars from a start bar until the end of a loop of 4-16 of its bars,
  then that loop again and again (``bed_candidates``: the bars and loop that keep the bed in S0's
  key, loop back naturally at a matching bar line, move between chords and open on the tonic) -
  the same sequence through the handoffs, where it is muted. In each handoff the bed is
  replaced by the **entering** song's instrumental (the **leaving** song's when the entering
  song is S0), in that song's shift (S0's key), at the ``H`` consecutive bars of it whose chords
  agree best with the bed's there (a bonus when they are the bars under its own vocal).
* **Instrumental leads.** A song without a sung vocal (``lead == 'other'``) carries its melody
  with its ``other`` stem, as in the chain; wherever such a lead sounds the instrumental under it
  is reduced to its rhythm section (drums + bass). A root without a sung vocal cannot give up
  its whole ``other`` stem to the bed and to its melody at once: its melody is its
  ``other-high`` stem (above middle C) and its bed drums + bass + ``other-low``.

``chord_match`` of a run is the mean over its bars of the agreement between the instrumental
playing and each vocal sounding there (both as heard, in S0's key).

Pure bar arithmetic: no audio here (see ``duet_render.py`` for seconds).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from ..paths.plan import wrap
from .chain import (COVER_WEIGHT, HALF_BAR_PENALTY, PHRASE_BONUS, SHIFT_PENALTY, bar_agreement, bar_sequence,
                    choose_start, loop_period, shift_label, triad)
from .tracks import fold_factor

LOOP_CANDIDATES = (4, 6, 8, 12, 16)   # loop periods (bars); no 2-bar loops for a 20 s pair
VOICE_COVER_WEIGHT = 5 * COVER_WEIGHT     # two vocals at all times: singing bars count more here
VARIETY_WEIGHT = 0.05                 # share of distinct source bars in a window (less looping)
JUMP_WEIGHT = 0.1                     # a vocal loop whose end runs naturally back into its start
OWN_BARS_BONUS = 0.05                 # a borrowed instrumental under its own song's vocal bars
TONIC_BONUS = 0.03                    # the bed opening on the root's tonic chord
HARMONY_WEIGHT = 0.05                 # a bed loop that moves between chords (up to 4 of them)
PHRASE_LOOP_BONUS = 0.08              # a bed loop of whole phrases (8 bars; 12 for a 12-bar blues)
SLOW_ROOT_BPM = 80.0                  # a root counted slower than this may be counted twice as fast
BED_CANDIDATES = 5                    # beds planned in full; the best plan wins (``plan_quality``)
BED_FIT_WEIGHT = 0.5                  # ... counting the bed's key fit this much
RHYTHM = ("rhythm",)
FULL = ("rhythm", "other")
ROOT_SPLIT = ("rhythm", "other-low")  # the bed of a root whose melody is its ``other`` stem
ROOT_OTHER_LEAD = "other-high"        # ... and that melody


@dataclass(frozen=True)
class Voice:
    song: int                         # index into DuetPlan.tracks
    start: int                        # first mix bar of the window (unwrapped: may be < 0)
    src_bars: tuple[int, ...]         # its source bars, 2P + H of them
    shift: int                        # semitones applied (into S0's key)
    key_shift: int                    # the key-derived shift
    chord_match: float                # mean agreement with the bed under the window
    per_bar: tuple                    # per window bar (None: silent)
    cover: float                      # vocal-active share of the window
    lead: str                         # 'vocals' | 'other'
    loop: tuple[int, int] | None = None   # source bars [a, e) repeated once the window reaches e
    stem: str = "vocals"              # the stem rendered: vocals, other (other-high for the root)

    @property
    def end(self) -> int:
        return self.start + len(self.src_bars)


@dataclass(frozen=True)
class Run:
    kind: str                         # 'duet' | 'handoff'
    pair: int                         # duet: its pair; handoff: the pair it leads into
    start: int                        # first mix bar
    bars: int
    song: int                         # instrumental (0 = the bed)
    src_bars: tuple[int, ...]
    stems: tuple[str, ...]            # FULL or RHYTHM
    vocals: tuple[int, int]           # the pair singing at the run's end
    entering: int | None
    leaving: int | None
    chord_match: float
    per_bar: tuple

    @property
    def end(self) -> int:
        return self.start + self.bars


@dataclass
class DuetPlan:
    tracks: list                      # the songs as planned (regridded / phase-shifted); [0] = root
    pair_bars: int                    # P
    handoff_bars: int                 # H
    bed: list[int]                    # S0's source bar under every mix bar (N of them)
    voices: list[Voice]               # one per song, in path order
    runs: list[Run]                   # contiguous: duet 0, handoff 1, duet 1, ..., duet n-1, handoff 0
    periods: list[int]
    phrase_bars: int
    variant_cost: list[float] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    @property
    def n_songs(self) -> int:
        return len(self.tracks)

    @property
    def n_bars(self) -> int:
        return self.n_songs * self.pair_bars

    def voice_offset(self, j: int, o: int) -> int | None:
        """Index into song j's window of mix bar ``o`` (circular), or None outside it."""
        v = self.voices[j]
        k = (o - v.start) % self.n_bars
        return k if k < len(v.src_bars) else None

    def voices_at(self, o: int) -> list[int]:
        """Songs whose vocal sounds in mix bar ``o`` (two; three inside a handoff)."""
        return [j for j in range(self.n_songs) if self.voice_offset(j, o) is not None]

    def run_at(self, o: int) -> Run:
        o %= self.n_bars
        return next(r for r in self.runs if r.start <= o < r.end)

    def inst_at(self, o: int) -> tuple[int, int, int]:
        """(song, source bar, shift) of the instrumental playing in mix bar ``o``."""
        r = self.run_at(o)
        return r.song, r.src_bars[o % self.n_bars - r.start], self.voices[r.song].shift if r.song else 0


# --------------------------------------------------------------------------- arithmetic
def pair_songs(n: int, k: int) -> tuple[int, int]:
    """The two songs singing pair ``k``."""
    return k % n, (k + 1) % n


def window_start(j: int, P: int, H: int) -> int:
    """First mix bar (unwrapped) of song j's vocal window."""
    return (j - 1) * P - H


def handoff_roles(n: int, m: int) -> tuple[int, int, int, int]:
    """(staying, entering, leaving, borrowed instrumental) of the handoff into pair ``m``."""
    staying, entering = pair_songs(n, m)
    leaving = (m - 1) % n
    return staying, entering, leaving, (entering if entering != 0 else leaving)


def heard(chords: list[int], shift: int) -> list[int]:
    return [shift_label(c, shift) for c in chords]


def agreement_table(targets: list[list[int]], bars: list[list[int]]) -> np.ndarray:
    """T[s, i, y] = agreement of target bar i with source bar y transposed by s (NaN: silent)."""
    T = np.full((12, len(targets), len(bars)), np.nan)
    for s in range(12):
        for i, a in enumerate(targets):
            for y, b in enumerate(bars):
                v = bar_agreement(a, b, s)
                if v is not None:
                    T[s, i, y] = v
    return T


def _nanmean(a: np.ndarray, axis: int) -> np.ndarray:
    cnt = np.sum(np.isfinite(a), axis=axis)
    return np.where(cnt > 0, np.nansum(a, axis=axis) / np.maximum(cnt, 1), 0.0)


# --------------------------------------------------------------------------- searches
@dataclass
class _Best:
    obj: float
    match: float
    b0: int
    shift: int
    per: list
    cover: float
    seq: list[int]
    loop: tuple[int, int] | None = None


def loop_sequence(start: int, length: int, loop: tuple[int, int]) -> list[int]:
    """Source bars start, start+1, ...; on reaching ``loop[1]`` (exclusive) they go back to
    ``loop[0]``: the bars of the loop repeat for as long as needed."""
    a, e = loop
    out, b = [], start
    for _ in range(max(0, length)):
        if b >= e:
            b = a
        out.append(b)
        b += 1
    return out


def jump_quality(own: list[list[int]], loop: tuple[int, int]) -> float:
    """How naturally the loop's end runs back into its start: the agreement of its last bar with
    the bar that precedes its first (or of the bar after its end with its first)."""
    a, e = loop
    v = None
    if a >= 1:
        v = bar_agreement(own[e - 1], own[a - 1])
    elif e < len(own):
        v = bar_agreement(own[e], own[a])
    return 0.5 if v is None else float(v)


def search_window(V, targets: list[list[int]], *, shifts, key_shift: int, lead: str) -> _Best | None:
    """Best (window start, loop, shift) of song ``V`` under ``targets`` (one bar's chords per
    window bar). The window plays V's bars from ``b0`` and, on reaching the end ``e`` of a loop
    ``[a, e)`` of ``LOOP_CANDIDATES`` bars (anywhere in the preview, ``b0 < e``), repeats that
    loop. Objective: chord agreement, minus ``SHIFT_PENALTY`` off the key-derived
    shift, plus bonuses for vocal cover, a window (and a loop) opening a sung phrase, distinct
    bars and a loop whose end runs naturally into its start (``JUMP_WEIGHT``)."""
    nV, W = V.n_bars, len(targets)
    if nV == 0 or W == 0:
        return None
    own = [V.bar_chords(y) for y in range(nV)]
    T = agreement_table(targets, own)
    voc = np.array([V.bar_vocal(y) for y in range(nV)]) if lead == "vocals" else np.ones(nV)
    shifts = list(shifts)
    sidx = np.asarray([s % 12 for s in shifts])[:, None]
    cost = np.array([0.0 if wrap(s) == key_shift else SHIFT_PENALTY for s in shifts])
    widx = np.arange(W)[None, :]

    def opens(b: int) -> bool:
        return lead == "vocals" and voc[b] >= 0.5 and (b == 0 or voc[b - 1] < 0.25)

    loops = sorted({(e - J, e) for J in LOOP_CANDIDATES for e in range(J, nV + 1)} or {(0, nV)})
    best: _Best | None = None
    seen: set[tuple[int, ...]] = set()
    for lp in loops:
        jq = jump_quality(own, lp)
        for b0 in range(lp[1]):
            seq = loop_sequence(b0, W, lp)
            key = tuple(seq)
            if key in seen:
                continue
            seen.add(key)
            vals = T[sidx, widx, np.asarray(seq)[None, :]]
            match = _nanmean(vals, axis=1)
            cover = float(np.mean(voc[seq]))
            looped = len(set(seq)) < W
            bonus = (VOICE_COVER_WEIGHT * cover + PHRASE_BONUS * (opens(b0) + (looped and opens(lp[0])))
                     + VARIETY_WEIGHT * len(set(seq)) / W + (JUMP_WEIGHT * jq if looped else JUMP_WEIGHT))
            obj = match - cost + bonus
            si = int(np.argmax(obj))
            if best is None or obj[si] > best.obj + 1e-12:
                per = [None if not np.isfinite(v) else round(float(v), 4) for v in vals[si]]
                best = _Best(float(obj[si]), float(match[si]), b0, int(wrap(shifts[si])), per, cover, list(seq),
                             lp if looped else None)
    return best


def search_borrow(X, targets: list[list[int]], *, shift: int, period: int, own_bars: list[int] | None) -> _Best:
    """Best ``len(targets)`` consecutive bars of song ``X`` (transposed by ``shift``) under the
    bed's chords ``targets``; ``OWN_BARS_BONUS`` when they are the bars under its own vocal."""
    nX, H = X.n_bars, len(targets)
    own = [X.bar_chords(y) for y in range(nX)]
    T = agreement_table(targets, own)[shift % 12]
    best: _Best | None = None
    for c0 in range(nX):
        seq = bar_sequence(c0, H, nX, period)
        vals = T[np.arange(H), np.asarray(seq)]
        match = float(_nanmean(vals[None, :], axis=1)[0])
        obj = match + (OWN_BARS_BONUS if own_bars is not None and list(seq) == list(own_bars) else 0.0)
        if best is None or obj > best.obj + 1e-12:
            best = _Best(obj, match, c0, shift, [None if not np.isfinite(v) else round(float(v), 4) for v in vals],
                         1.0, list(seq))
    return best


def key_fit(label: int, tonic: int, mode: str) -> float | None:
    """1 when the chord's triad lies in the key's scale (natural minor plus its major V), 0.5
    when two of its tones do, 0 otherwise; None for a silent slot."""
    if label < 0:
        return None
    steps = (0, 2, 3, 5, 7, 8, 10) if mode == "minor" else (0, 2, 4, 5, 7, 9, 11)
    scale = {(tonic + k) % 12 for k in steps}
    if mode == "minor" and label == ((tonic + 7) % 12) * 2:
        return 1.0
    inside = len(triad(label) & scale)
    return 1.0 if inside == 3 else 0.5 if inside == 2 else 0.0


@dataclass(frozen=True)
class BedChoice:
    start: int
    loop: tuple[int, int]
    bars: tuple[int, ...]             # the root's source bar under every mix bar
    fit: float                        # mean key_fit of its half-bar chords
    score: float


def bed_candidates(root, n_bars: int, keep: int = BED_CANDIDATES, phrase_bars: int = 8) -> list[BedChoice]:
    """Ways to lay the root's instrumental under ``n_bars`` mix bars: its bars from a start bar
    until the end of a loop ``[a, e)`` of ``LOOP_CANDIDATES`` bars, then that loop again and
    again. Ranked by how well the bed stays in the root's key (``key_fit`` of every half-bar
    chord it plays), how naturally its loop runs back into its start (``jump_quality``), whether
    it moves between chords, how many distinct bars it uses, whether it and its loop open on the
    tonic chord and whether the loop is a whole number of phrases (``PHRASE_LOOP_BONUS``; a
    phrase longer than 8 bars - the 12-bar blues - must be looped whole); the best ``keep`` with different loops, plus the chain's default (the first bar of
    0..3 on the tonic, looping the last ``loop_period`` bars)."""
    nR = root.n_bars
    own = [root.bar_chords(b) for b in range(nR)]
    fit = []
    for ch in own:
        v = [x for x in (key_fit(c, root.tonic, root.mode) for c in ch) if x is not None]
        fit.append((sum(v), len(v)))
    tonic = root.tonic * 2 + (1 if root.mode == "minor" else 0)

    def opens(b: int) -> bool:
        return bool(own[b]) and own[b][0] == tonic

    def make(s0: int, lp: tuple[int, int]) -> BedChoice:
        seq = loop_sequence(s0, n_bars, lp)
        num, den = sum(fit[b][0] for b in seq), sum(fit[b][1] for b in seq)
        f = num / den if den else 0.0
        harmony = len({c for b in range(*lp) for c in own[b] if c >= 0})
        score = (f + JUMP_WEIGHT * jump_quality(own, lp) + HARMONY_WEIGHT * min(harmony, 4) / 4
                 + VARIETY_WEIGHT * len(set(seq)) / n_bars + TONIC_BONUS * (opens(s0) + opens(lp[0]))
                 + PHRASE_LOOP_BONUS * ((lp[1] - lp[0]) % phrase_bars == 0))
        return BedChoice(s0, lp, tuple(seq), round(f, 4), score)

    lengths = LOOP_CANDIDATES
    if phrase_bars > 8 and nR >= phrase_bars:          # a 12-bar form loops whole choruses
        lengths = tuple(J for J in range(phrase_bars, nR + 1, phrase_bars))
    loops = sorted({(e - J, e) for J in lengths for e in range(J, nR + 1)} or {(0, nR)})
    best_per_loop = [max((make(s0, lp) for s0 in range(lp[1])), key=lambda c: c.score) for lp in loops]
    ranked = sorted(best_per_loop, key=lambda c: -c.score)[:keep]
    period = min(loop_period(own, phrase_bars, LOOP_CANDIDATES), nR)
    default = make(choose_start(root, min_left=min(nR, 8)), (nR - period, nR))
    if all(c.bars != default.bars for c in ranked):
        ranked.append(default)
    return ranked


def variants(track, root, half_bar: bool) -> list[tuple[object, float]]:
    """(counting of ``track``, objective cost): the tempo octave nearest the root's, and the same
    with its bar lines half a bar later."""
    v = track.regrid(fold_factor(track.bpm, root.bpm))
    out = [(v, 0.0)]
    if half_bar and v.bpb % 2 == 0 and v.bpb >= 4:
        out.append((v.shift_phase(v.bpb // 2), HALF_BAR_PENALTY))
    return out


# --------------------------------------------------------------------------- plan
def plan_duet(tracks: list, *, pair_seconds: float = 20.0, handoff_bars: int = 2, phrase_bars: int = 8,
              half_bar: bool = True) -> DuetPlan:
    n = len(tracks)
    if n < 3:
        raise ValueError("a duet loop needs at least three songs")
    H = int(handoff_bars)
    if H < 1:
        raise ValueError("handoff_bars must be at least 1")
    plans = []
    for root in root_countings(tracks):
        if root.n_bars < 2:
            continue
        P = max(H + 2, int(round(pair_seconds / root.bar_seconds)))
        songs = [root, *tracks[1:]]
        plans += [_plan_with_bed(songs, bed, P=P, H=H, phrase_bars=phrase_bars, half_bar=half_bar,
                                 pair_seconds=pair_seconds) for bed in bed_candidates(root, len(tracks) * P, phrase_bars=phrase_bars)]
    if not plans:
        raise ValueError(f"the root song has only {tracks[0].n_bars} bars")
    return max(plans, key=plan_quality)


def root_countings(tracks: list) -> list:
    """The root as measured, and - when that counting is slower than ``SLOW_ROOT_BPM`` (e.g. a
    compound meter counted in dotted beats) - also in the tempo octave nearest the other songs'
    median tempo."""
    root = tracks[0]
    out = [root]
    if root.bpm < SLOW_ROOT_BPM:
        f = fold_factor(root.bpm, float(np.median([t.bpm for t in tracks[1:]])))
        if f != 1.0:
            out.append(root.regrid(f))
    return out


def plan_quality(plan: DuetPlan) -> float:
    """Chord agreement of the runs (per bar), the bed's key fit (``BED_FIT_WEIGHT``) and the
    vocal cover of the windows (``VOICE_COVER_WEIGHT``)."""
    bars = sum(r.bars for r in plan.runs)
    match = sum(r.chord_match * r.bars for r in plan.runs) / max(bars, 1)
    cover = float(np.mean([v.cover for v in plan.voices]))
    return match + BED_FIT_WEIGHT * plan.params["bed_fit"] + VOICE_COVER_WEIGHT * cover


def _plan_with_bed(tracks: list, choice: BedChoice, *, P: int, H: int, phrase_bars: int, half_bar: bool,
                   pair_seconds: float) -> DuetPlan:
    n = len(tracks)
    root = tracks[0]
    N = n * P
    W = 2 * P + H
    s0, bed_loop, bed = choice.start, choice.loop, list(choice.bars)
    period0 = bed_loop[1] - bed_loop[0]
    bed_chords = [root.bar_chords(b) for b in bed]

    planned = [root]
    periods = [period0]
    costs = [0.0]
    voices: list[Voice] = []
    for j, t in enumerate(tracks):
        st = window_start(j, P, H)
        targets = [bed_chords[(st + k) % N] for k in range(W)]
        if j == 0:
            opts, shifts, key_shift = [(root, 0.0)], [0], 0
        else:
            opts, shifts, key_shift = variants(t, root, half_bar), range(-6, 6), wrap(root.frame_shift - t.frame_shift)
        best, pick = None, None
        for V, c in opts:
            if V.n_bars < 1:
                continue
            per = loop_period([V.bar_chords(b) for b in range(V.n_bars)], phrase_bars, LOOP_CANDIDATES)
            b = search_window(V, targets, shifts=shifts, key_shift=key_shift, lead=t.lead)
            if b is None:
                continue
            b.obj -= c
            if best is None or b.obj > best.obj:
                best, pick = b, (V, per, c)
        if best is None:
            raise ValueError(f"song {j + 1} ({t.work_id}) has no usable bars")
        if j:
            planned.append(pick[0])
            periods.append(pick[1])
            costs.append(pick[2])
        else:
            periods[0] = pick[1]
        voices.append(Voice(song=j, start=st, src_bars=tuple(best.seq), shift=best.shift, key_shift=key_shift,
                            chord_match=round(best.match, 4), per_bar=tuple(best.per), cover=round(best.cover, 3),
                            lead=t.lead, loop=best.loop,
                            stem=(ROOT_OTHER_LEAD if j == 0 else "other") if t.lead == "other" else "vocals"))
    other_leads = {j for j, v in enumerate(voices) if v.lead == "other" and j}

    def stems_for(song: int, sounding: set[int]) -> tuple[str, ...]:
        if sounding & other_leads or (song and planned[song].lead == "other"):
            return RHYTHM
        return ROOT_SPLIT if song == 0 and root.lead == "other" else FULL

    plan = DuetPlan(tracks=planned, pair_bars=P, handoff_bars=H, bed=bed, voices=voices, runs=[], periods=periods,
                    phrase_bars=phrase_bars, variant_cost=costs,
                    params=dict(pair_seconds=pair_seconds, handoff_bars=H, start_bar=s0, bed_loop=bed_loop, bed_fit=choice.fit,
                                half_bar=half_bar))
    runs: list[Run] = []
    for k in range(n):
        a = k * P
        sounding = {s for o in range(a, a + P - H) for s in plan.voices_at(o)}
        runs.append(Run("duet", k, a, P - H, 0, tuple(bed[a:a + P - H]), stems_for(0, sounding), pair_songs(n, k),
                        None, None, 0.0, ()))
        m = (k + 1) % n
        staying, entering, leaving, X = handoff_roles(n, m)
        h0 = a + P - H
        targets = bed_chords[h0:h0 + H]
        own = None
        if X == entering:
            own = list(voices[X].src_bars[:H])
        elif X == leaving:
            own = list(voices[X].src_bars[-H:])
        b = search_borrow(planned[X], targets, shift=voices[X].shift, period=periods[X], own_bars=own)
        sounding = {s for o in range(h0, h0 + H) for s in plan.voices_at(o)}
        runs.append(Run("handoff", m, h0, H, X, tuple(b.seq), stems_for(X, sounding), (staying, entering),
                        entering, leaving, 0.0, ()))
    plan.runs = runs
    plan.runs = [_with_match(plan, r) for r in runs]
    return plan


def bar_match(plan: DuetPlan, o: int) -> float | None:
    """Mean agreement, in mix bar ``o``, of the instrumental with every vocal sounding there."""
    song, src, shift = plan.inst_at(o)
    inst = heard(plan.tracks[song].bar_chords(src), shift)
    vals = []
    for j in plan.voices_at(o):
        v = plan.voices[j]
        k = plan.voice_offset(j, o)
        a = bar_agreement(inst, heard(plan.tracks[j].bar_chords(v.src_bars[k]), v.shift))
        if a is not None:
            vals.append(a)
    return float(np.mean(vals)) if vals else None


def _with_match(plan: DuetPlan, r: Run) -> Run:
    per = [bar_match(plan, o) for o in range(r.start, r.end)]
    vals = [v for v in per if v is not None]
    return replace(r, chord_match=round(float(np.mean(vals)), 4) if vals else 0.0,
                   per_bar=tuple(None if v is None else round(v, 4) for v in per))
