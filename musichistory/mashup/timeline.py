"""Seconds for a chain plan: the mix's beat grid, every song's source-to-mix time maps
(pieces), transposition curves and stem envelopes.

* **Grid.** A full or changeover bar lasts as long as the bar of the song whose backing
  plays in it (its native beats, so the backing is never stretched). A morph run of bars takes
  its song's own beat intervals times a ratio gliding (smoothstep over the run's beats) from
  ``r0`` = the previous bar's beat interval in the mix / the song's beat interval, to 1.
* **Pieces.** A song's bars in the mix (as backing or lead) split into pieces wherever the
  mix bars or the source bars stop being consecutive (a loop jump) or the song switches
  between native (its own clock, untransposed: copied sample for sample) and warped
  (changeover lead, morph: time-mapped beat by beat). Each piece maps the source beats of its
  bars onto the mix beats of its bars.
* **Pitch.** A song's lead is transposed by its hop's ``shift`` plus the tuning difference
  (backing's deviation from A440 minus its own) through the changeover and glides to 0 over its
  morph (smoothstep in mix time).
* **Envelopes.** Per (song, stem) the mix intervals where it sounds: full and morph bars play
  the song's ``vocals`` + ``instruments``; a changeover plays the backing song's
  ``instruments`` (``drums`` + ``bass`` when the lead is an instrument) and the lead song's lead
  stem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..paths.plan import smoothstep
from .chain import ChainPlan


@dataclass
class Piece:
    song: int
    out_bars: list[int]
    src_bars: list[int]
    warped: bool
    src_times: np.ndarray            # source seconds of the bars' beats (+ the last bar's end)
    out_times: np.ndarray            # the matching mix seconds
    join_prev: str | None = None     # 'cont' (same source continues) | 'jump' | None
    join_next: str | None = None

    @property
    def out_start(self) -> float:
        return float(self.out_times[0])

    @property
    def out_end(self) -> float:
        return float(self.out_times[-1])


@dataclass
class Timeline:
    plan: ChainPlan
    grid: np.ndarray                 # mix seconds of every beat, len = bars * bpb + 1
    bpb: int
    pieces: list[Piece]
    envelopes: dict[tuple[int, str], list[tuple[float, float]]]
    shifts: dict[int, int]           # song -> semitones of its lead in its changeover
    morphs: dict[int, tuple[float, float]]   # song -> mix seconds of its morph
    detune: dict[int, float] = field(default_factory=dict)   # song -> backing tuning - own tuning

    @property
    def seconds(self) -> float:
        return float(self.grid[-1])

    def bar_span(self, o: int) -> tuple[float, float]:
        return float(self.grid[o * self.bpb]), float(self.grid[(o + 1) * self.bpb])

    def semitones(self, song: int, t: float) -> float:
        """Transposition of ``song`` at mix time ``t``: its hop's shift plus the tuning
        difference to the backing, gliding to 0 over its morph."""
        s = self.shifts.get(song, 0) + self.detune.get(song, 0.0)
        if not s:
            return 0.0
        m = self.morphs.get(song)
        if m is None or t <= m[0]:
            return float(s)
        if t >= m[1]:
            return 0.0
        return float(s) * (1.0 - smoothstep((t - m[0]) / (m[1] - m[0])))

    def song_pieces(self, song: int) -> list[Piece]:
        return [p for p in self.pieces if p.song == song]

    def mix_to_source(self, song: int, t):
        """Source seconds heard at mix time(s) ``t`` from ``song`` (NaN outside its pieces)."""
        t = np.atleast_1d(np.asarray(t, dtype=float))
        out = np.full(t.shape, np.nan)
        for p in self.song_pieces(song):
            sel = (t >= p.out_start) & (t < p.out_end)
            out[sel] = np.interp(t[sel], p.out_times, p.src_times)
        return out

    def beat_position(self, t):
        """Fractional mix beat index at mix time(s) ``t``."""
        idx = np.arange(len(self.grid), dtype=float)
        return np.interp(t, self.grid, idx)


def build_grid(plan: ChainPlan) -> np.ndarray:
    tracks = plan.tracks
    bpb = tracks[0].bpb
    grid = [0.0]
    o = 0
    bars = plan.bars
    while o < len(bars):
        ob = bars[o]
        if ob.kind != "morph":
            t = tracks[ob.inst[0]].bar_times(ob.inst[1])
            for d in np.diff(t):
                grid.append(grid[-1] + float(d))
            o += 1
            continue
        run = o
        while run < len(bars) and bars[run].kind == "morph" and bars[run].inst[0] == ob.inst[0]:
            run += 1
        song = tracks[ob.inst[0]]
        d = np.concatenate([np.diff(song.bar_times(bars[k].inst[1])) for k in range(o, run)])
        prev = np.diff(grid[-bpb - 1:]) if len(grid) > bpb else d
        r0 = float(np.median(prev)) / float(np.median(d))
        n = len(d)
        for j, dj in enumerate(d):
            r = r0 + (1.0 - r0) * smoothstep((j + 0.5) / n)
            grid.append(grid[-1] + float(dj) * r)
        o = run
    return np.asarray(grid, dtype=float)


def _role(ob, song: int) -> tuple[int, bool] | None:
    """(source bar, warped) of ``song`` in out bar ``ob`` or None."""
    if ob.inst[0] == song:
        return ob.inst[1], ob.kind == "morph"
    if ob.vocal is not None and ob.vocal[0] == song:
        return ob.vocal[1], True
    return None


def build(plan: ChainPlan) -> Timeline:
    tracks = plan.tracks
    bpb = tracks[0].bpb
    grid = build_grid(plan)
    pieces: list[Piece] = []
    for j, tr in enumerate(tracks):
        rows = [(o, *_role(ob, j)) for o, ob in enumerate(plan.bars) if _role(ob, j) is not None]
        runs: list[list[tuple[int, int, bool]]] = []
        for row in rows:
            if runs:
                o0, s0, w0 = runs[-1][-1]
                if row[0] == o0 + 1 and row[1] == s0 + 1 and row[2] == w0:
                    runs[-1].append(row)
                    continue
            runs.append([row])
        song_pieces = []
        for run in runs:
            src, out = [], []
            for o, s, _ in run:
                src.extend(tr.bar_times(s)[:-1])
                out.extend(grid[o * bpb:(o + 1) * bpb])
            o_last, s_last, _ = run[-1]
            src.append(tr.bar_times(s_last)[-1])
            out.append(grid[(o_last + 1) * bpb])
            song_pieces.append(Piece(j, [r[0] for r in run], [r[1] for r in run], run[0][2],
                                     np.asarray(src, dtype=float), np.asarray(out, dtype=float)))
        for a, b in zip(song_pieces, song_pieces[1:]):
            if b.out_bars[0] == a.out_bars[-1] + 1:
                kind = "cont" if b.src_bars[0] == a.src_bars[-1] + 1 else "jump"
                a.join_next = b.join_prev = kind
        pieces.extend(song_pieces)

    env: dict[tuple[int, str], list[list[float]]] = {}

    def on(song: int, stem: str, o: int) -> None:
        t0, t1 = float(grid[o * bpb]), float(grid[(o + 1) * bpb])
        ivs = env.setdefault((song, stem), [])
        if ivs and abs(ivs[-1][1] - t0) < 1e-9:
            ivs[-1][1] = t1
        else:
            ivs.append([t0, t1])

    for o, ob in enumerate(plan.bars):
        if ob.kind in ("full", "morph"):
            on(ob.inst[0], "vocals", o)
            on(ob.inst[0], "instruments", o)
        else:
            lead = tracks[ob.vocal[0]].lead
            if lead == "vocals":
                on(ob.inst[0], "instruments", o)
            else:
                on(ob.inst[0], "drums", o)
                on(ob.inst[0], "bass", o)
            on(ob.vocal[0], lead, o)

    shifts, morphs, detune = {}, {}, {}
    for h in plan.hops:
        shifts[h.b] = h.shift
        detune[h.b] = round(float(tracks[h.a].tuning - tracks[h.b].tuning), 3)
        m = [o for o, ob in enumerate(plan.bars) if ob.kind == "morph" and ob.inst[0] == h.b]
        if m:
            morphs[h.b] = (float(grid[m[0] * bpb]), float(grid[(m[-1] + 1) * bpb]))
    return Timeline(plan=plan, grid=grid, bpb=bpb, pieces=pieces,
                    envelopes={k: [tuple(v) for v in vs] for k, vs in env.items()}, shifts=shifts, morphs=morphs,
                    detune=detune)
