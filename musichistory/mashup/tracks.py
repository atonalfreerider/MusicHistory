"""A path song as the chain planner sees it: a regular bar grid on its measured beats, the
accompaniment's chroma and chords, vocal activity per beat, its key, and an optional
tempo-octave regrid.

Bars are complete only: bar ``k`` spans beats ``phase + k*bpb`` .. ``phase + (k+1)*bpb`` and
needs that last beat to exist. **Bar chords** are read per half bar (two per 4-beat bar): the
accompaniment's beat chroma summed over the half bar, matched against the triad/seventh
templates and Viterbi-smoothed over the song (``paths.audio.chord_labels``) - steadier than
single-beat readings. ``regrid(2)`` inserts a beat between every two (the song is counted in
twice as many beats; its bars become half bars), ``regrid(0.5)`` keeps every other beat from a
downbeat (bars become two original bars). A bar of 3 or 6 tracker beats faster than
``COMPOUND_BPM`` is compound meter (12/8 heard in its subdivisions): the tracker's bars become
the beats. Any other meter is regrouped into 4-beat bars at the phase where chords change most.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from functools import cached_property

import numpy as np

from ..identity.key import MAJOR, MINOR, key_name
from ..paths.audio import chord_labels
from ..paths.identity import frame_shift
from .analysis import ACTIVE_DB, unpack_u8

PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
COMPOUND_BPM = 140.0    # a 3- or 6-beat bar counted faster than this is compound (12/8, 6/8)
NO_VOCAL_DB = -20.0     # a vocal stem this far under the mix (RMS) is residue, not singing
HALF_PENALTY = 0.08     # Viterbi change penalty between half-bar chords


def parse_key(name: str) -> tuple[int, str]:
    """'C# minor' -> (1, 'minor')."""
    note, mode = name.split()
    pc = PITCH[note[0]] + note[1:].count("#") - note[1:].count("b")
    return pc % 12, (MINOR if mode == MINOR else MAJOR)


@dataclass
class Track:
    work_id: str
    tonic: int
    mode: str
    beats: np.ndarray                 # source seconds
    bpb: int
    phase: int                        # index of the first downbeat in ``beats``
    chroma: np.ndarray                # 12 x len(beats) accompaniment chroma per beat
    vocal: np.ndarray                 # per beat: 1.0 vocal active, 0.0 not
    duration: float
    title: str = ""
    artist: str = ""
    year: int = 0
    factor: float = 1.0               # regrid factor applied (2: beats doubled, 0.5: halved)
    lead: str = "vocals"              # stem that carries the melody ('other' when no vocal)
    tuning: float = 0.0               # deviation from A440 in semitones (-0.5 .. 0.5)
    meta: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ grid
    @property
    def n_bars(self) -> int:
        return max(0, (len(self.beats) - 1 - self.phase) // self.bpb)

    def bar_beat(self, bar: int, beat: int = 0) -> int:
        return self.phase + bar * self.bpb + beat

    def bar_start(self, bar: int) -> float:
        return float(self.beats[self.bar_beat(bar)])

    def bar_times(self, bar: int) -> np.ndarray:
        """bpb + 1 beat times of bar ``bar`` (start of each beat and the bar's end)."""
        i = self.bar_beat(bar)
        return np.asarray(self.beats[i:i + self.bpb + 1], dtype=float)

    @property
    def halves(self) -> list[tuple[int, int]]:
        """Beat ranges (within a bar) of its chord slots: two halves, or the whole bar."""
        if self.bpb >= 4 and self.bpb % 2 == 0:
            h = self.bpb // 2
            return [(0, h), (h, self.bpb)]
        return [(0, self.bpb)]

    @cached_property
    def _bar_labels(self) -> list[list[int]]:
        cols, n = [], self.n_bars
        for bar in range(n):
            i = self.bar_beat(bar)
            for a, b in self.halves:
                cols.append(self.chroma[:, i + a:i + b].sum(axis=1) if self.chroma.shape[1] >= i + b
                            else np.zeros(12))
        if not cols:
            return []
        labels = chord_labels(np.stack(cols, axis=1), penalty=HALF_PENALTY)
        k = len(self.halves)
        return [labels[j * k:(j + 1) * k] for j in range(n)]

    def bar_chords(self, bar: int) -> list[int]:
        """Chord labels (root*2 + minor; -1 silent) of the bar's half-bar slots."""
        return list(self._bar_labels[bar]) if 0 <= bar < len(self._bar_labels) else []

    def bar_vocal(self, bar: int) -> float:
        i = self.bar_beat(bar)
        v = self.vocal[i:i + self.bpb]
        return float(np.mean(v)) if len(v) else 0.0

    @property
    def ibi(self) -> float:
        d = np.diff(self.beats)
        return float(np.median(d)) if len(d) else 0.5

    @property
    def bpm(self) -> float:
        return 60.0 / self.ibi

    @property
    def bar_seconds(self) -> float:
        return self.ibi * self.bpb

    @property
    def frame_shift(self) -> int:
        """Add to a C-major/A-minor frame pitch class to get this recording's pitch class."""
        return frame_shift(self.tonic, self.mode)

    @property
    def key_name(self) -> str:
        return key_name(self.tonic, self.mode)

    @property
    def vocal_share(self) -> float:
        return float(np.mean(self.vocal)) if len(self.vocal) else 0.0

    def local_ibi(self, bar: int) -> float:
        t = self.bar_times(bar)
        return float(np.median(np.diff(t))) if len(t) > 1 else self.ibi

    # ------------------------------------------------------------------ regrid
    def regrid(self, factor: float) -> "Track":
        if factor == 1:
            return self
        b = np.asarray(self.beats, dtype=float)
        if factor == 2:
            mids = (b[:-1] + b[1:]) / 2
            nb = np.empty(len(b) + len(mids))
            nb[0::2], nb[1::2] = b, mids
            ch = np.repeat(self.chroma, 2, axis=1)[:, : len(nb)] / 2
            vo = np.repeat(self.vocal, 2)[: len(nb)]
            return replace(self, beats=nb, chroma=ch, vocal=vo, phase=self.phase * 2, factor=self.factor * 2)
        if factor == 0.5:
            start = self.phase % 2
            return self._group(2, start, phase=(self.phase - start) // 2, factor=self.factor / 2)
        raise ValueError(f"regrid factor must be 0.5, 1 or 2, not {factor}")

    def shift_phase(self, beats: int) -> "Track":
        """The same song with its bar lines moved ``beats`` beats later (a mid-bar alignment)."""
        if beats == 0:
            return self
        return replace(self, phase=self.phase + beats,
                       meta={**self.meta, "phase_shift": self.meta.get("phase_shift", 0) + beats})

    def _group(self, step: int, start: int, **changes) -> "Track":
        nb = np.asarray(self.beats, dtype=float)[start::step]
        idx = range(start, len(self.beats), step)
        ch = np.stack([self.chroma[:, i:i + step].sum(axis=1) for i in idx], axis=1) if len(nb) else np.zeros((12, 0))
        vo = np.array([float(np.max(self.vocal[i:i + step])) if len(self.vocal[i:i + step]) else 0.0 for i in idx])
        return replace(self, beats=nb, chroma=ch[:, : len(nb)], vocal=vo[: len(nb)], **changes)


def fold_factor(bpm: float, target: float) -> float:
    """0.5, 1 or 2: the regrid that brings ``bpm`` nearest to ``target`` (log distance)."""
    return min((1.0, 2.0, 0.5), key=lambda f: abs(math.log2(bpm * f / target)))


def change_phase(labels: list[int], bpb: int) -> int:
    """Bar phase at which the chord changes most often (bar lines carry the changes)."""
    hits = np.zeros(bpb)
    for i in range(1, len(labels)):
        if labels[i] >= 0 and labels[i] != labels[i - 1]:
            hits[i % bpb] += 1
    return int(np.argmax(hits)) if hits.any() else 0


def from_analysis(work_id: str, doc: dict, key: str, *, bpb: int = 4, title: str = "", artist: str = "",
                  year: int = 0) -> Track:
    tonic, mode = parse_key(key)
    beats = np.asarray(doc["beats"], dtype=float)
    chroma = unpack_u8(doc.get("beat_chroma") or "")
    if chroma.shape[1] < len(beats):
        chroma = np.hstack([chroma, np.zeros((12, len(beats) - chroma.shape[1]))])
    vdb = np.asarray(doc.get("vocal_db") or [], dtype=float)
    vv = np.asarray(doc.get("vocal_voiced") or [], dtype=float)
    vocal = ((vdb > ACTIVE_DB) & (vv >= 0.2)).astype(float) if len(vdb) else np.zeros(len(beats))
    vocal = np.r_[vocal, np.zeros(max(0, len(beats) - len(vocal)))][: len(beats)]
    levels = doc.get("levels_db") or {}
    if levels and levels.get("vocals", 0) - levels.get("mix", 0) < NO_VOCAL_DB:
        vocal = np.zeros(len(beats))          # the 'vocal' stem is separation residue only
    own = int(doc.get("beats_per_bar") or 4)
    phase = int(doc.get("downbeat_phase") or 0)
    t = Track(work_id=work_id, tonic=tonic, mode=mode, beats=beats, bpb=own, phase=phase % own, chroma=chroma,
              vocal=vocal, duration=float(doc.get("duration") or 0.0), title=title, artist=artist, year=year,
              meta={"beats_per_bar_measured": own, "meter": "measured", "levels_db": levels},
              tuning=float(doc.get("tuning") or 0.0))
    if own != bpb:
        if own in (3, 6) and t.bpm > COMPOUND_BPM:
            t = t._group(3, t.phase % 3, phase=0)
            t.meta = {**t.meta, "meter": "compound"}
        else:
            t.meta = {**t.meta, "meter": "regrouped"}
        beat_labels = chord_labels(t.chroma)
        t = replace(t, bpb=bpb, phase=change_phase(beat_labels, bpb))
    if t.vocal_share < 0.08:
        t.lead = "other"
    return t
