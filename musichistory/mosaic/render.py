"""Audio of a mosaic: the target loop played as many whole times as fit in ``MAX_SECONDS``.

* **Grid.** The loop is the target's own bars, native (never stretched): loop beat ``q`` sounds
  at the target's beat ``beat0 + q`` less the loop's start. Beyond the loop the grid continues
  periodically (``LoopGrid``), so a loop ``k`` later is ``k * T`` seconds later.
* **Sections** (``sections``): **original** (target instrumental + target vocal) -> **mosaic**
  (target instrumental + the pieces' vocals) -> **harmony** (target instrumental + target vocal
  + the harmony voices): exactly one original loop (the intro), the remaining loops split
  between mosaic (the larger share, about 60 %) and harmony.
* **Tiles.** The bed (the target's ``instruments`` stem), the target vocal and every harmony
  voice are rendered once as a circular loop tile (``circular_tile``): the audio a little
  before and after the loop is laid on, equal-power crossfaded over ``JOIN`` (bed) or
  ``VOICE_JOIN`` (voices) around the loop boundary and wrapped, so repeating the tile is
  seamless. A harmony voice is one span of its song warped beat by beat onto the loop (Rubber
  Band, ``mashup.warp``, formants preserved) and transposed by its shift plus the tuning
  difference to the target.
* **Pieces.** Each piece's source notes are warped onto the target's beat grid by its fold and
  offset (time map points every half source beat) and transposed (formant-preserving) by its
  shift plus the tuning difference. Its source is heard as a continuous phrase: it enters
  ``LEAD_BEATS`` (one beat) of its own audio before its first matched note and keeps singing
  past its last until the next piece enters (around the loop circularly), at most
  ``TAIL_BARS`` bar beyond its last note (silence after that); neighbours share one cut point
  (the later piece's full lead-in wherever there is room), and every cut is an equal-power
  crossfade of ``XF`` seconds. The rendered pieces are added at every mosaic loop.
* **Levels.** Bed and target vocal as the preview (``mashup.render.song_gain``); every piece is
  scaled to the target vocal's active level (``mashup.duet_render.active_db``, at most
  ``PIECE_MAX_DB`` away) and every harmony voice to ``HARMONY_REL_DB`` under it, panned
  ``-PAN`` / ``+PAN`` (constant power). The target vocal and the harmonies enter and leave
  with equal-power fades of half a beat (at most ``mashup.render.MAX_FADE``) centred on the
  section boundaries; 20 ms fade-in, the last beat fades out. Loudness as every mix
  (``mashup.render.write_mp3``: -16 LUFS, true peak held under -1 dBTP).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..mashup import render as mrender
from ..mashup.duet_render import active_db, add_circular
from ..mashup.warp import TimeMap, warp
from .notes import to_seconds

SR = mrender.SR
MAX_SECONDS = 90.0
JOIN = 0.010
VOICE_JOIN = 0.03
XF = 0.06
LEAD_BEATS = 1.0             # a piece's own audio before its first matched note
TAIL_BARS = 1.0              # ... and at most this long after its last one
MIN_LEAD = 0.03              # seconds of a piece always heard before its first note ...
MIN_TAIL = 0.08              # ... and after its last
MARGIN = 0.3
PIECE_MAX_DB = 12.0
HARMONY_REL_DB = -3.0
PAN = 0.35
MIN_LOOPS = 3


@dataclass
class LoopGrid:
    """Loop seconds of loop beats 0..n (times[0] = 0, times[-1] = the loop's length)."""

    times: np.ndarray

    @property
    def seconds(self) -> float:
        return float(self.times[-1])

    @property
    def n_beats(self) -> int:
        return len(self.times) - 1

    def time(self, q):
        q = np.asarray(q, dtype=float)
        k = np.floor(q / self.n_beats)
        r = q - k * self.n_beats
        return np.interp(r, np.arange(len(self.times), dtype=float), self.times) + k * self.seconds

    def beat(self, t):
        t = np.asarray(t, dtype=float)
        k = np.floor(t / self.seconds)
        r = t - k * self.seconds
        return np.interp(r, self.times, np.arange(len(self.times), dtype=float)) + k * self.n_beats


def loop_grid(beats: np.ndarray, beat0: int, n_beats: int) -> LoopGrid:
    b = np.asarray(beats[beat0:beat0 + n_beats + 1], dtype=float)
    return LoopGrid(b - b[0])


@dataclass
class PieceSpec:
    work_id: str
    src_beats: np.ndarray              # the source's beat grid (s)
    tuning: float
    fold: float
    offset: float                      # loop beat of source beat 0
    shift: int
    first: float                       # loop beat of its first note's onset
    last: float                        # loop beat of its last note's end
    duration: float = 0.0              # source preview seconds (0: unknown)


@dataclass
class VoiceSpec:
    work_id: str
    src_beats: np.ndarray
    tuning: float
    fold: float
    start: float                       # source beat heard at loop beat 0
    shift: int
    duration: float = 0.0


@dataclass
class Spec:
    target: str
    beats: np.ndarray                  # the target's beat grid (s)
    beat0: int
    n_beats: int                       # loop beats
    bpb: int
    tuning: float
    levels: dict
    pieces: list[PieceSpec]
    voices: list[VoiceSpec]
    max_seconds: float = MAX_SECONDS


@dataclass
class Rendered:
    mix: np.ndarray
    buses: dict[str, np.ndarray]
    grid: LoopGrid
    n_loops: int
    sections: list[tuple[str, int, int]]          # (kind, first loop, loops)
    cuts: list[tuple[float, float]]               # per piece (cut in, cut out), loop seconds
    joins: list[float]                            # mix seconds of every join to check
    gains: dict = field(default_factory=dict)

    @property
    def seconds(self) -> float:
        return self.n_loops * self.grid.seconds

    def section_span(self, kind: str) -> tuple[float, float]:
        k, a, n = next(s for s in self.sections if s[0] == kind)
        return a * self.grid.seconds, (a + n) * self.grid.seconds


# --------------------------------------------------------------------------- plan
def sections(n_loops: int) -> list[tuple[str, int, int]]:
    """(kind, first loop, loops): one original loop (the intro), then the mosaic (about 60 % of
    the rest, rounded up) and the harmonies (the remainder, at least one loop)."""
    if n_loops < MIN_LOOPS:
        raise ValueError(f"{n_loops} loops: a mosaic needs at least {MIN_LOOPS}")
    o = 1
    rest = n_loops - o
    m = min(rest - 1, int(np.ceil(0.6 * rest)))
    h = rest - m
    return [("original", 0, o), ("mosaic", o, m), ("harmony", o + m, h)]


def loops_in(seconds: float, loop_seconds: float) -> int:
    return int(np.floor(seconds / loop_seconds + 1e-9))


def piece_cuts(spans: list[tuple[float, float]], T: float, lead: float, max_tail: float
               ) -> list[tuple[float, float]]:
    """(cut in, cut out) in loop seconds of every piece from its (first onset, last end), sorted
    by onset. A piece enters ``lead`` before its first note; the piece before it (around the loop)
    keeps singing until then - at most ``max_tail`` past its own last note, so far-apart pieces
    leave silence between them - and the two share that cut. Where the gap is shorter than the
    lead, the cut moves later (``MIN_TAIL`` after the earlier piece's last note, but at least
    ``MIN_LEAD`` before the later one's first)."""
    n = len(spans)
    cuts = [[on - lead, off + max_tail] for on, off in spans]
    for i in range(n):
        j = (i + 1) % n
        wrap = j <= i
        off_i = spans[i][1]
        on_j = spans[j][0] + (T if wrap else 0.0)
        enter = on_j - lead
        if off_i + max_tail < enter:
            continue
        c = min(max(enter, off_i + MIN_TAIL), on_j - MIN_LEAD)
        cuts[i][1] = c
        cuts[j][0] = c - (T if wrap else 0.0)
    return [(float(x), float(y)) for x, y in cuts]


# --------------------------------------------------------------------------- audio helpers
def window(n: int, i_in: float, i_out: float, xf: int) -> np.ndarray:
    """Gain rising 0 -> 1 (equal power, sine) over ``xf`` samples centred on sample ``i_in`` and
    falling 1 -> 0 over ``xf`` samples centred on ``i_out``; two such windows meeting at one
    cut sum to constant power."""
    k = np.arange(n) + 0.5
    h = xf / 2.0
    up = np.clip((k - (i_in - h)) / xf, 0.0, 1.0)
    down = np.clip(((i_out + h) - k) / xf, 0.0, 1.0)
    return (np.sin(0.5 * np.pi * up) * np.sin(0.5 * np.pi * down)).astype(np.float32)


def circular_tile(y: np.ndarray, t0: float, T: float, join: float, sr: int = SR) -> np.ndarray:
    """A loop tile of ``T`` seconds from audio ``y`` starting at loop time ``t0`` (< 0): kept
    from -join/2 to T + join/2 with equal-power ramps there, the overhang wrapped around."""
    n = int(round(T * sr))
    buf = np.zeros((n, y.shape[1]), dtype=np.float32)
    i0 = int(round(t0 * sr))
    xf = max(2, int(round(join * sr)))
    w = window(len(y), -i0, n - i0, xf)
    add_circular(buf, (y * w[:, None]).astype(np.float32), i0)
    return buf


def native_tile(audio: np.ndarray, src_t0: float, T: float, join: float = JOIN, sr: int = SR) -> np.ndarray:
    """The source audio from ``src_t0`` as a loop tile (copied sample for sample)."""
    m = min(MARGIN, src_t0)
    a = int(round((src_t0 - m) * sr))
    b = int(round((src_t0 + T + MARGIN) * sr))
    y = audio[a:b]
    need = b - a
    if len(y) < need:
        y = np.vstack([y, np.zeros((need - len(y), audio.shape[1]), np.float32)])
    return circular_tile(y, -m, T, join, sr)


def map_points(src_beats: np.ndarray, grid: LoopGrid, fold: float, offset: float, x0: float, x1: float,
               duration: float) -> TimeMap | None:
    """Time map (source s -> loop s) for loop beats x0..x1, one point per half source beat."""
    step = 0.5 * fold
    xs = np.r_[np.arange(x0, x1, step), x1]
    src = np.asarray(to_seconds((xs - offset) / fold, src_beats), dtype=float)
    dst = np.asarray(grid.time(xs), dtype=float)
    ok = (src >= 0.0) & (src <= (duration if duration > 0 else np.inf) - 0.01)
    src, dst = src[ok], dst[ok]
    if len(src) < 2:
        return None
    keep = np.r_[True, (np.diff(src) > 1e-6) & (np.diff(dst) > 1e-6)]
    src, dst = src[keep], dst[keep]
    return TimeMap(src, dst) if len(src) >= 2 else None


def render_span(audio: np.ndarray, tmap: TimeMap, lo: float, hi: float, semis: float, sr: int = SR
                ) -> tuple[np.ndarray, float]:
    """``audio`` through ``tmap`` for loop seconds [lo, hi] (clipped to the map): (y, start s)."""
    m = mrender.crop_map(tmap.src, tmap.dst, lo, hi)
    s0 = int(round(m.src[0] * sr))
    seg = audio[s0:int(np.ceil(m.src[-1] * sr)) + 1]
    y = warp(seg, sr, m, semis, formant=True)
    return y, float(m.dst[0])


def render_piece(audio: np.ndarray, p: PieceSpec, grid: LoopGrid, cut: tuple[float, float], semis: float,
                 sr: int = SR) -> tuple[np.ndarray, float]:
    """One piece, gated by its cuts: (audio, loop seconds of its first sample)."""
    lo, hi = cut[0] - XF, cut[1] + XF
    x0 = float(grid.beat(lo - MARGIN)) - 0.5
    x1 = float(grid.beat(hi + MARGIN)) + 0.5
    tmap = map_points(p.src_beats, grid, p.fold, p.offset, x0, x1, p.duration or len(audio) / sr)
    if tmap is None or min(hi, tmap.dst[-1]) - max(lo, tmap.dst[0]) < 0.01:
        return np.zeros((0, audio.shape[1]), np.float32), lo
    y, t0 = render_span(audio, tmap, lo, hi, semis, sr)
    w = window(len(y), (cut[0] - t0) * sr, (cut[1] - t0) * sr, max(2, int(round(XF * sr))))
    return (y * w[:, None]).astype(np.float32), t0


def voice_tile(audio: np.ndarray, v: VoiceSpec, grid: LoopGrid, semis: float, sr: int = SR) -> np.ndarray:
    """A harmony voice's span warped over the loop, as a loop tile."""
    margin_beats = 1.0
    T = grid.seconds
    tmap = map_points(v.src_beats, grid, v.fold, -v.start * v.fold, -margin_beats, grid.n_beats + margin_beats,
                      v.duration or len(audio) / sr)
    if tmap is None:
        return np.zeros((int(round(T * sr)), audio.shape[1]), np.float32)
    lo = max(float(tmap.dst[0]), -VOICE_JOIN)
    hi = min(float(tmap.dst[-1]), T + VOICE_JOIN)
    if hi - lo < 0.01:
        return np.zeros((int(round(T * sr)), audio.shape[1]), np.float32)
    y, t0 = render_span(audio, tmap, lo, hi, semis, sr)
    return circular_tile(y, t0, T, VOICE_JOIN, sr)


def pan(y: np.ndarray, p: float) -> np.ndarray:
    ang = 0.25 * np.pi * (1.0 + p)
    g = np.sqrt(2.0) * np.array([np.cos(ang), np.sin(ang)], dtype=np.float32)
    return (y * g[None, :]).astype(np.float32)


def section_env(n: int, spans: list[tuple[float, float]], fade: float, total: float, sr: int = SR) -> np.ndarray:
    """1 inside the spans (seconds), equal-power fades of ``fade`` centred on inner boundaries."""
    g = np.zeros(n, dtype=np.float32)
    t = np.arange(n) / sr
    for a, b in spans:
        fa = 0.0 if a <= 1e-6 else fade
        fb = 0.0 if b >= total - 1e-6 else fade
        u_in = np.ones(n) if fa == 0 else np.clip((t - (a - fa / 2)) / fa, 0, 1)
        u_out = np.ones(n) if fb == 0 else np.clip(((b + fb / 2) - t) / fb, 0, 1)
        inside = (t >= a - fa / 2) & (t < b + fb / 2)
        g = np.maximum(g, np.where(inside, np.sin(0.5 * np.pi * u_in) * np.sin(0.5 * np.pi * u_out), 0)
                       .astype(np.float32))
    return g


def tile(y: np.ndarray, k: int) -> np.ndarray:
    return np.tile(y, (k, 1))


# --------------------------------------------------------------------------- render
def render(spec: Spec, stems, log=None, sr: int = SR) -> Rendered:
    """Every bus and the mix (before loudness normalization). ``stems.get(work_id, stem)``
    returns (n, 2) float32 at ``sr`` (``mashup.render.StemCache``)."""
    grid = loop_grid(spec.beats, spec.beat0, spec.n_beats)
    T = grid.seconds
    n_loops = loops_in(spec.max_seconds, T)
    secs = sections(n_loops)
    n_tile = int(round(T * sr))
    n = n_tile * n_loops
    total = n / sr
    src_t0 = float(spec.beats[spec.beat0])
    g_song = mrender.song_gain(spec.levels)

    bed_tile = native_tile(stems.get(spec.target, "instruments"), src_t0, T) * np.float32(g_song)
    voc_tile = native_tile(stems.get(spec.target, "vocals"), src_t0, T, VOICE_JOIN) * np.float32(g_song)
    ref_db = active_db(voc_tile)
    ref_db = ref_db if ref_db is not None else mrender.REF_DB - 4.0
    if log:
        log(f"    bed and target vocal tiles ({T:.2f}s loop, {n_loops} loops)")

    # pieces
    spans = [(float(grid.time(p.first)), float(grid.time(p.last))) for p in spec.pieces]
    order = np.argsort([s[0] for s in spans])
    beat = float(np.median(np.diff(grid.times)))
    cuts_sorted = piece_cuts([spans[i] for i in order], T, LEAD_BEATS * beat, TAIL_BARS * spec.bpb * beat)
    cuts: list[tuple[float, float]] = [(0.0, 0.0)] * len(spans)
    for k, i in enumerate(order):
        cuts[i] = cuts_sorted[k]
    piece_audio: list[tuple[np.ndarray, float]] = []
    gains: dict = {"song": round(g_song, 4), "pieces": [], "voices": []}
    for p, cut in zip(spec.pieces, cuts):
        semis = p.shift + (spec.tuning - p.tuning)
        y, t0 = render_piece(stems.get(p.work_id, "vocals"), p, grid, cut, semis, sr)
        a = active_db(y) if len(y) else None
        g = 1.0 if a is None else float(10 ** (np.clip(ref_db - a, -PIECE_MAX_DB, PIECE_MAX_DB) / 20))
        gains["pieces"].append(round(g, 4))
        piece_audio.append((y * np.float32(g), t0))
        if log:
            log(f"    piece {p.work_id} {cut[0]:.2f}-{cut[1]:.2f}s shift {p.shift:+d} x{g:.2f}")

    # voices
    voice_tiles = []
    for k, v in enumerate(spec.voices):
        semis = v.shift + (spec.tuning - v.tuning)
        y = voice_tile(stems.get(v.work_id, "vocals"), v, grid, semis, sr)
        a = active_db(y)
        g = 1.0 if a is None else float(10 ** (np.clip(ref_db + HARMONY_REL_DB - a, -PIECE_MAX_DB, PIECE_MAX_DB) / 20))
        gains["voices"].append(round(g, 4))
        voice_tiles.append(pan(y * np.float32(g), -PAN if k % 2 == 0 else PAN))
        if log:
            log(f"    harmony {v.work_id} shift {v.shift:+d} x{g:.2f}")

    # sections
    span = {kind: (a * T, (a + m) * T) for kind, a, m in secs}
    fade = min(0.5 * beat, mrender.MAX_FADE)
    bed = tile(bed_tile, n_loops)
    tv = tile(voc_tile, n_loops) * section_env(n, [span["original"], span["harmony"]], fade, total, sr)[:, None]
    harm = np.zeros((n, 2), np.float32)
    henv = section_env(n, [span["harmony"]], fade, total, sr)[:, None]
    buses = {"bed": bed, "target_vocal": tv}
    for k, vt in enumerate(voice_tiles):
        hb = tile(vt, n_loops) * henv
        buses[f"harmony_{k}"] = hb
        harm += hb
    pcs = np.zeros((n, 2), np.float32)
    _, m0, m_n = next(s for s in secs if s[0] == "mosaic")
    joins: list[float] = [k * T for k in range(1, n_loops)]
    for loop_k in range(m0, m0 + m_n):
        base = loop_k * T
        for (y, t0), cut in zip(piece_audio, cuts):
            i0 = int(round((base + t0) * sr))
            a, b = max(i0, 0), min(i0 + len(y), n)
            if b > a:
                pcs[a:b] += y[a - i0:b - i0]
            joins += [base + cut[0], base + cut[1]]
    buses["pieces"] = pcs
    mix = (bed + tv + harm + pcs).astype(np.float32)
    k = int(mrender.FADE_IN * sr)
    mix[:k] *= np.linspace(0, 1, k, dtype=np.float32)[:, None]
    a = int(round((total - beat) * sr))
    if a < n:
        u = np.linspace(0, 1, n - a, dtype=np.float32)
        mix[a:] *= (np.cos(0.5 * np.pi * u) ** 2)[:, None]
    joins = sorted({round(j, 4) for j in joins if 0.5 < j < total - beat - 0.5})
    return Rendered(mix, buses, grid, n_loops, secs, cuts, joins, gains)


def mix_beats(r: Rendered) -> list[tuple[float, float]]:
    """(mix seconds, loop beat) of every beat of the mix."""
    out = []
    T = r.grid.seconds
    for k in range(r.n_loops):
        for q in range(r.grid.n_beats):
            out.append((k * T + float(r.grid.times[q]), float(q)))
    return out
