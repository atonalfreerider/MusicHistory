"""Handoff plan of one path step: the clip starts in the key and tempo heard at the end of the
previous clip and glides to its own.

Mirrors ``Morph.Plan(previous, next, morphBars, heardBpm, entryFileBpm)`` in
``unity/Assets/MusicHistory/Contracts/ISongPlayer.cs`` with the recordings' measured values:

* ``start_semitones = Wrap(previous tonic - this tonic)`` in [-6, 5] (tonic to tonic, the mode
  is not considered, exactly as Unity);
* ``start_bpm`` = the previous clip's tempo as heard (every clip ends at its own measured
  tempo), halved/doubled toward this clip's tempo only when more than ``FOLD_OCTAVES`` apart;
  the start tempo ratio (rubberband's ``tempo`` factor) is ``start_bpm / bpm``;
* the glide covers ``morph_bars`` bars of this song's meter and follows a smoothstep.

Unity counts the glide in the file's beats; a recording has no beat clock, so the contract
(``paths.json``) states the glide in **output seconds**: ``morph_seconds = morph_bars ·
beats_per_bar · 60 / start_bpm`` (the bars at the start tempo), with progress
``s = smoothstep(t / morph_seconds)`` at output time ``t``. The heard tempo is then
``start_bpm + (bpm - start_bpm)·s`` and the transposition ``start_semitones·(1 - s)``.
``input_time`` integrates the tempo ratio so the renderer can place its commands on the
source's timeline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MORPH_BARS = 2.0
FOLD_OCTAVES = 0.8
COMMAND_STEP = 0.025       # seconds of output between glide commands


def wrap(semitones: int) -> int:
    """Morph.Wrap: any semitone difference into [-6, 5]."""
    return ((int(semitones) % 12) + 12 + 6) % 12 - 6


def fold_bpm(heard: float, target: float) -> float:
    """Morph.Plan's octave fold: ``heard`` as is unless more than FOLD_OCTAVES from ``target``;
    then the nearest of heard/2, heard*2, heard/4, heard*4 (log distance) toward it."""
    f = target if target > 0 else 120.0
    p = heard if heard > 0 else f
    best = p
    if abs(math.log2(p / f)) > FOLD_OCTAVES:
        for c in (p / 2, p * 2, p / 4, p * 4):
            if abs(math.log(c / f)) < abs(math.log(best / f)):
                best = c
    return best


def smoothstep(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return u * u * (3 - 2 * u)


@dataclass(frozen=True)
class StepPlan:
    start_semitones: int
    start_bpm: float
    bpm: float
    beats_per_bar: float = 4.0
    morph_bars: float = MORPH_BARS
    morph: bool = True          # False for a path's first step (plays natively)

    @property
    def start_ratio(self) -> float:
        """Playback tempo over the recording's own at the first output sample."""
        return self.start_bpm / self.bpm if self.bpm > 0 else 1.0

    @property
    def morph_seconds(self) -> float:
        if not self.morph or self.morph_bars <= 0 or self.start_bpm <= 0:
            return 0.0
        return self.morph_bars * (self.beats_per_bar if self.beats_per_bar > 0 else 4.0) * 60.0 / self.start_bpm

    @property
    def is_identity(self) -> bool:
        """Nothing to glide: same tonic and (within 0.1 %) the same tempo."""
        return self.start_semitones == 0 and abs(self.start_ratio - 1.0) < 1e-3

    def progress(self, t: float) -> float:
        T = self.morph_seconds
        return 1.0 if T <= 0 else smoothstep(t / T)

    def semitones_at(self, t: float) -> float:
        return self.start_semitones * (1.0 - self.progress(t))

    def bpm_at(self, t: float) -> float:
        """Tempo heard at output time ``t``."""
        return self.start_bpm + (self.bpm - self.start_bpm) * self.progress(t)

    def ratio_at(self, t: float) -> float:
        r0 = self.start_ratio
        return r0 + (1.0 - r0) * self.progress(t)

    def input_time(self, t: float) -> float:
        """Source seconds consumed after ``t`` output seconds: the integral of ``ratio_at``.
        With S(u) = u^3 - u^4/2 (the integral of smoothstep), it is r0·t + (1-r0)·T·S(t/T)
        during the glide and T·(1+r0)/2 + (t - T) after it."""
        T = self.morph_seconds
        if T <= 0:
            return t
        r0 = self.start_ratio
        if t >= T:
            return T * (1.0 + r0) / 2.0 + (t - T)
        u = max(0.0, t / T)
        return r0 * t + (1.0 - r0) * T * (u ** 3 - u ** 4 / 2.0)

    def commands(self, step: float = COMMAND_STEP) -> list[tuple[float, float, float]]:
        """(source time, rubberband tempo, rubberband pitch ratio) every ``step`` output
        seconds through the glide, ending exactly at (native tempo, native pitch)."""
        T = self.morph_seconds
        if T <= 0 or self.is_identity:
            return []
        out = []
        k = 1
        while k * step < T:
            t = k * step
            out.append((self.input_time(t), self.ratio_at(t), 2.0 ** (self.semitones_at(t) / 12.0)))
            k += 1
        out.append((self.input_time(T), 1.0, 1.0))
        return out


def plan_step(prev_tonic: int | None, prev_bpm: float | None, tonic: int, bpm: float, *,
              beats_per_bar: float = 4.0, morph_bars: float = MORPH_BARS) -> StepPlan:
    """The plan of a clip after one heard in ``prev_tonic`` at ``prev_bpm`` (None: first step)."""
    if prev_tonic is None or prev_bpm is None:
        return StepPlan(0, float(bpm), float(bpm), beats_per_bar, morph_bars, morph=False)
    return StepPlan(wrap(int(prev_tonic) - int(tonic)), float(fold_bpm(float(prev_bpm), float(bpm))), float(bpm),
                    beats_per_bar, morph_bars, morph=True)
