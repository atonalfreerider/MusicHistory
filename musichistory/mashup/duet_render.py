"""Seconds and audio for a duet plan (``duet.py``): a circular mix whose last sample runs on
into its first.

* **Grid.** Mix bar o lasts as long as the root's bed bar ``plan.bed[o]`` (its native beats:
  the bed is never stretched). Mix bars and beats are counted circularly; an *unwrapped*
  bar/beat (negative, or past the end) lies a whole number of loop lengths away.
* **Elements.** Every sounding thing is an element on the unwrapped timeline: the bed of each
  duet run (the root's instrumental, native: copied sample for sample), the borrowed
  instrumental of each handoff, and each song's vocal window (both warped beat by beat onto the
  grid with Rubber Band and transposed by the song's shift plus the tuning difference to the
  root, formants preserved for vocals). An element's bars split into pieces at loop jumps,
  which meet with 20 ms equal-power crossfades (``render.render_stem``'s joins).
* **Gains.** Instrumentals cross over at the handoff edges with equal-power fades of half a
  beat (at most ``render.MAX_FADE``) centred on the bar line; a vocal fades in over its first
  handoff and out over its last (equal power, so leaving + entering keep constant power), and
  pans from ``+PAN`` (the newer voice of its first pair) to ``-PAN`` (the older voice of its
  second pair) over the handoff between them (smoothstep, constant-power pan law). Each
  rendered element is added into its bus modulo the loop length, so whatever runs past the
  end (or starts before 0) sounds at the start (or the end): the loop is seamless by
  construction.
* **Levels.** Instrumentals as in the chain (each song's preview scaled to ``render.REF_DB``);
  each lead is scaled so its active level (RMS over its frames within 20 dB of its loud level)
  is ``LEAD_REL_DB`` under that reference (at most ``LEAD_MAX_DB`` away from the song's own
  scaling), so both voices are heard alike. The loop is normalized to ``render.TARGET_LUFS``
  and peak-limited circularly (``write_loop_mp3``); no fades.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import render
from .duet import DuetPlan, Run
from .timeline import Piece
from .warp import warp

SR = render.SR
PAN = 0.25                    # vocal positions: newer voice right, older voice left
LEAD_REL_DB = -4.0            # a lead's active level re render.REF_DB (median natural vocal ~ -3.7 dB re mix)
LEAD_MAX_DB = 9.0
ACTIVE_RANGE_DB = 20.0
JOIN = render.JOIN
MARGIN = render.MARGIN


@dataclass
class Element:
    song: int
    role: str                          # 'bed' | 'borrowed' | 'lead'
    stems: tuple[str, ...]
    pieces: list[Piece]
    ramp_in: tuple[float, float]       # unwrapped seconds: gain rises 0 -> 1 (equal power)
    ramp_out: tuple[float, float]      # gain falls 1 -> 0
    semitones: float = 0.0
    pan: tuple[float, float, float, float] | None = None   # (t0, t1, p0, p1): glide from p0 to p1
    run: int | None = None             # index into plan.runs (instrumentals)


@dataclass
class DuetTimeline:
    plan: DuetPlan
    grid: np.ndarray                   # mix seconds of every beat, N*bpb + 1 (grid[-1] = loop length)
    bpb: int
    elements: list[Element] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return float(self.grid[-1])

    @property
    def n_beats(self) -> int:
        return len(self.grid) - 1

    def beat_time(self, q):
        """Unwrapped seconds of (fractional) unwrapped beat(s) ``q``."""
        q = np.asarray(q, dtype=float)
        k = np.floor(q / self.n_beats)
        r = q - k * self.n_beats
        return np.interp(r, np.arange(len(self.grid), dtype=float), self.grid) + k * self.seconds

    def bar_time(self, o: int) -> float:
        return float(self.beat_time(o * self.bpb))

    def beat_of(self, t):
        """Unwrapped (fractional) beat at unwrapped seconds ``t``."""
        t = np.asarray(t, dtype=float)
        k = np.floor(t / self.seconds)
        r = t - k * self.seconds
        return np.interp(r, self.grid, np.arange(len(self.grid), dtype=float)) + k * self.n_beats

    def fade_len(self, t: float) -> float:
        q = float(self.beat_of(t))
        beat = float(self.beat_time(np.floor(q) + 1) - self.beat_time(np.floor(q)))
        return min(0.5 * beat, render.MAX_FADE)


# --------------------------------------------------------------------------- build
def build_grid(plan: DuetPlan) -> np.ndarray:
    root = plan.tracks[0]
    d = np.concatenate([np.diff(root.bar_times(b)) for b in plan.bed])
    return np.r_[0.0, np.cumsum(d)]


def pieces(tl: DuetTimeline, song: int, rows: list[tuple[int, int]], warped: bool) -> list[Piece]:
    """Pieces of ``song`` for (unwrapped mix bar, source bar) rows: runs where both advance by
    one, joined 'cont' or 'jump'."""
    tr = tl.plan.tracks[song]
    bpb = tl.bpb
    runs: list[list[tuple[int, int]]] = []
    for row in rows:
        if runs and row[0] == runs[-1][-1][0] + 1 and row[1] == runs[-1][-1][1] + 1:
            runs[-1].append(row)
        else:
            runs.append([row])
    out: list[Piece] = []
    for run in runs:
        src = np.concatenate([tr.bar_times(s)[:-1] for _, s in run] + [tr.bar_times(run[-1][1])[-1:]])
        o0, o1 = run[0][0], run[-1][0] + 1
        dst = tl.beat_time(np.arange(o0 * bpb, o1 * bpb + 1))
        out.append(Piece(song, [o for o, _ in run], [s for _, s in run], warped, np.asarray(src, float),
                         np.asarray(dst, float)))
    for a, b in zip(out, out[1:]):
        if b.out_bars[0] == a.out_bars[-1] + 1:
            a.join_next = b.join_prev = "cont" if b.src_bars[0] == a.src_bars[-1] + 1 else "jump"
    return out


def build(plan: DuetPlan) -> DuetTimeline:
    root = plan.tracks[0]
    tl = DuetTimeline(plan=plan, grid=build_grid(plan), bpb=root.bpb)
    P, H = plan.pair_bars, plan.handoff_bars
    for ri, r in enumerate(plan.runs):
        rows = [(r.start + k, s) for k, s in enumerate(r.src_bars)]
        t0, t1 = tl.bar_time(r.start), tl.bar_time(r.end)
        f0, f1 = tl.fade_len(t0), tl.fade_len(t1)
        if r.kind == "duet":
            el = Element(0, "bed", r.stems, pieces(tl, 0, rows, warped=False), (t0 - f0 / 2, t0 + f0 / 2),
                         (t1 - f1 / 2, t1 + f1 / 2), 0.0, run=ri)
        else:
            semis = plan.voices[r.song].shift + (root.tuning - plan.tracks[r.song].tuning)
            el = Element(r.song, "borrowed", r.stems, pieces(tl, r.song, rows, warped=True),
                         (t0 - f0 / 2, t0 + f0 / 2), (t1 - f1 / 2, t1 + f1 / 2), float(semis), run=ri)
        tl.elements.append(el)
    for v in plan.voices:
        rows = [(v.start + k, s) for k, s in enumerate(v.src_bars)]
        semis = 0.0 if v.song == 0 else v.shift + (root.tuning - plan.tracks[v.song].tuning)
        st = v.start
        tl.elements.append(Element(
            v.song, "lead", (v.stem,), pieces(tl, v.song, rows, warped=True),
            (tl.bar_time(st), tl.bar_time(st + H)), (tl.bar_time(v.end - H), tl.bar_time(v.end)), float(semis),
            pan=(tl.bar_time(st + P), tl.bar_time(st + P + H), PAN, -PAN)))
    return tl


# --------------------------------------------------------------------------- gains
def gain_at(el: Element, t: np.ndarray) -> np.ndarray:
    """The element's equal-power envelope at unwrapped seconds ``t``."""
    a, b = el.ramp_in
    c, d = el.ramp_out
    u_in = np.clip((t - a) / max(b - a, 1e-9), 0.0, 1.0)
    u_out = np.clip((d - t) / max(d - c, 1e-9), 0.0, 1.0)
    return (np.sin(0.5 * np.pi * u_in) * np.sin(0.5 * np.pi * u_out)).astype(np.float32)


def pan_gains(el: Element, t: np.ndarray) -> np.ndarray | None:
    """(len(t), 2) left/right gains of a panned element (constant power, unity at centre)."""
    if el.pan is None:
        return None
    t0, t1, p0, p1 = el.pan
    u = np.clip((t - t0) / max(t1 - t0, 1e-9), 0.0, 1.0)
    s = u * u * (3 - 2 * u)
    p = p0 + (p1 - p0) * s
    ang = 0.25 * np.pi * (1.0 + p)
    return (np.sqrt(2.0) * np.stack([np.cos(ang), np.sin(ang)], axis=1)).astype(np.float32)


def add_circular(buf: np.ndarray, y: np.ndarray, i0: int) -> None:
    """buf[(i0 + k) mod len(buf)] += y[k]."""
    n = len(buf)
    pos = 0
    while pos < len(y):
        i = (i0 + pos) % n
        take = min(len(y) - pos, n - i)
        buf[i:i + take] += y[pos:pos + take]
        pos += take


# --------------------------------------------------------------------------- audio
class DuetStems(render.StemCache):
    """``render.StemCache`` plus the virtual stem ``rhythm`` = drums + bass."""

    def get(self, song: int, stem: str) -> np.ndarray:
        if stem == "rhythm":
            key = (song, "rhythm")
            if key not in self.cache:
                self.cache[key] = super().get(song, "drums") + super().get(song, "bass")
            return self.cache[key]
        return super().get(song, stem)

    def mixed(self, song: int, stems: tuple[str, ...]) -> np.ndarray:
        if set(stems) == {"rhythm", "other"}:
            return self.get(song, "instruments")
        out = self.get(song, stems[0])
        for s in stems[1:]:
            out = out + self.get(song, s)
        return out


def active_db(y: np.ndarray, sr: int = SR, hop: int = 1024) -> float | None:
    """RMS level (dBFS) of a signal over its frames within ``ACTIVE_RANGE_DB`` of its loud
    level (95th percentile of frame RMS)."""
    m = y.mean(axis=1) if y.ndim == 2 else y
    k = len(m) // hop
    if k == 0:
        return None
    fr = np.sqrt(np.mean(m[:k * hop].astype(np.float64).reshape(k, hop) ** 2, axis=1)) + 1e-12
    db = 20 * np.log10(fr)
    loud = float(np.percentile(db, 95))
    if loud < -80:
        return None
    act = db > loud - ACTIVE_RANGE_DB
    return float(10 * np.log10(np.mean(fr[act] ** 2)))


def lead_gain(audio: np.ndarray, levels: dict) -> float:
    """Gain bringing a lead stem's active level to ``REF_DB + LEAD_REL_DB`` (within
    ``LEAD_MAX_DB`` of the song's own scaling)."""
    base = render.song_gain(levels)
    a = active_db(audio)
    if a is None:
        return base
    base_db = 20 * np.log10(base)
    want = render.REF_DB + LEAD_REL_DB - a
    return float(10 ** (np.clip(want, base_db - LEAD_MAX_DB, base_db + LEAD_MAX_DB) / 20))


def _is_identity(p: Piece, semitones: float) -> bool:
    return abs(semitones) < 1e-6 and np.allclose(np.diff(p.src_times), np.diff(p.out_times), atol=1e-6)


def render_element(tl: DuetTimeline, el: Element, audio: np.ndarray, buf: np.ndarray, gain: float = 1.0,
                   sr: int = SR) -> list[float]:
    """Render ``el`` from ``audio`` ((n, 2) at ``sr``) into the circular ``buf`` with its
    envelope, pan and ``gain``. Returns the (wrapped) mix times where two of its pieces meet."""
    src_end = len(audio) / sr
    a, d = el.ramp_in[0], el.ramp_out[1]
    joins: list[float] = []
    formant = el.role == "lead" and el.stems == ("vocals",)
    for p in el.pieces:
        s_ext, d_ext = render.extend(p.src_times, p.out_times, MARGIN, src_end)
        lo, hi = max(float(d_ext[0]), a - 0.01), min(float(d_ext[-1]), d + 0.01)
        if hi - lo < 1e-3:
            continue
        m = render.crop_map(s_ext, d_ext, lo, hi)
        i0 = int(round(m.dst[0] * sr))
        n_out = int(round((m.dst[-1] - m.dst[0]) * sr))
        s0 = int(round(m.src[0] * sr))
        if not p.warped or _is_identity(p, el.semitones):
            y = audio[s0:s0 + n_out]
            if len(y) < n_out:
                y = np.vstack([y, np.zeros((n_out - len(y), audio.shape[1]), np.float32)])
        else:
            seg = audio[s0:int(np.ceil(m.src[-1] * sr)) + 1]
            y = warp(seg, sr, m, el.semitones, formant=formant)
        w = np.ones(len(y), dtype=np.float32)
        j = int(round(JOIN * sr))
        if p.join_prev:
            c = int(round(p.out_start * sr)) - i0
            r = render.ramp(2 * j, "power" if p.join_prev == "jump" else "linear").astype(np.float32)
            lo_, hi_ = max(c - j, 0), max(min(c + j, len(w)), 0)
            w[:lo_] = 0.0
            if hi_ > lo_:
                w[lo_:hi_] = r[(lo_ - (c - j)):(hi_ - (c - j))]
            joins.append(p.out_start % tl.seconds)
        if p.join_next:
            c = int(round(p.out_end * sr)) - i0
            r = render.ramp(2 * j, "power" if p.join_next == "jump" else "linear")[::-1].astype(np.float32)
            lo_, hi_ = max(min(c - j, len(w)), 0), max(min(c + j, len(w)), 0)
            w[hi_:] = 0.0
            if hi_ > lo_:
                w[lo_:hi_] = np.minimum(w[lo_:hi_], r[(lo_ - (c - j)):(hi_ - (c - j))])
        t = (i0 + np.arange(len(y))) / sr
        g = gain_at(el, t) * w * np.float32(gain)
        out = y * g[:, None]
        pg = pan_gains(el, t)
        if pg is not None:
            out = out * pg
        add_circular(buf, out.astype(np.float32), i0)
    return joins


@dataclass
class Buses:
    bed: np.ndarray                    # every instrumental
    leads: dict[int, np.ndarray]       # song -> its lead, as mixed (gain, envelope, pan)
    joins: list[float]
    lead_gains: dict[int, float]

    @property
    def lead(self) -> np.ndarray:
        out = np.zeros_like(self.bed)
        for y in self.leads.values():
            out += y
        return out

    def mix(self) -> np.ndarray:
        return (self.bed + self.lead).astype(np.float32)


def render_buses(tl: DuetTimeline, stems: DuetStems, levels: dict[int, dict], sr: int = SR, log=None) -> Buses:
    n = int(round(tl.seconds * sr))
    bed = np.zeros((n, 2), dtype=np.float32)
    leads: dict[int, np.ndarray] = {}
    gains: dict[int, float] = {}
    joins: list[float] = []
    for el in tl.elements:
        audio = stems.mixed(el.song, el.stems)
        if el.role == "lead":
            g = lead_gain(audio, levels.get(el.song, {}))
            gains[el.song] = round(g, 4)
            buf = leads.setdefault(el.song, np.zeros((n, 2), dtype=np.float32))
        else:
            g = render.song_gain(levels.get(el.song, {}))
            buf = bed
        joins += render_element(tl, el, audio, buf, g, sr)
        if log:
            log(f"    rendered {tl.plan.tracks[el.song].work_id} {el.role} {'+'.join(el.stems)}")
    return Buses(bed, leads, sorted(set(round(j, 4) for j in joins)), gains)


# --------------------------------------------------------------------------- loudness / encode
def limit_circular(y: np.ndarray, sr: int, ceiling: float, pad: float = 1.0) -> np.ndarray:
    """``render.limit`` on the loop as a circle (its end runs into its start)."""
    k = min(len(y), int(pad * sr))
    ext = np.concatenate([y[-k:], y, y[:k]])
    return render.limit(ext, sr, ceiling)[k:k + len(y)]


def write_loop_mp3(ffmpeg: str, mix: np.ndarray, dst: Path, sr: int = SR) -> dict:
    """Normalize the loop to ``render.TARGET_LUFS`` (true peak held under ``MAX_TRUE_PEAK -
    TP_MARGIN`` by the circular limiter when needed) and encode it (libmp3lame VBR q2, LAME
    gapless header)."""
    import soundfile

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f"_{dst.stem}.tmp.wav"
    part = dst.parent / f"_{dst.stem}.part.mp3"
    try:
        soundfile.write(str(tmp), mix, sr, subtype="FLOAT")
        pre = render.loudness(ffmpeg, tmp)
        gain_db = render.TARGET_LUFS - pre["lufs"]
        limited = pre["true_peak"] + gain_db > render.MAX_TRUE_PEAK - render.TP_MARGIN
        y = (mix * np.float32(10 ** (gain_db / 20))).astype(np.float32)
        if limited:
            y = limit_circular(y, sr, 10 ** ((render.MAX_TRUE_PEAK - render.TP_MARGIN - render.LIMIT_HEADROOM) / 20))
        soundfile.write(str(tmp), y, sr, subtype="FLOAT")
        render._ffmpeg(ffmpeg, ["-y", "-v", "error", "-i", tmp.name, "-ar", str(sr), "-ac", "2", "-c:a", "libmp3lame",
                                "-q:a", "2", part.name], dst.parent)
        os.replace(part, dst)
        post = render.loudness(ffmpeg, dst)
        return {"gain_db": round(gain_db, 2), "limited": bool(limited), "pre": pre, "lufs": post["lufs"],
                "true_peak": post["true_peak"], "normalized": y}
    finally:
        tmp.unlink(missing_ok=True)
        part.unlink(missing_ok=True)


def run_seconds(tl: DuetTimeline, r: Run) -> tuple[float, float]:
    return tl.bar_time(r.start), tl.bar_time(r.end)

