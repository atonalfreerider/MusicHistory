"""Bar lines and metric positions from the slim ``measures`` runs.

Everything is in quarter-note beats. A run ``[start_beat, num, den, bars]`` has bars of
``num * 4 / den`` beats. The felt beat is a dotted quarter in compound meters (6/8, 9/8,
12/8), as in Resonance's chord grid, and ``4 / den`` otherwise.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

TOL = 1e-3


def beat_unit(num: int, den: int) -> float:
    if den == 8 and num % 3 == 0 and num > 3:
        return 1.5
    return 4.0 / den


@dataclass
class Meter:
    runs: list[tuple[float, int, int, int]]   # (start_beat, num, den, bars)

    @classmethod
    def from_slim(cls, slim: dict) -> Meter:
        runs = [(float(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in slim.get("measures") or () if r[1] > 0 and r[2] > 0]
        return cls(runs or [(0.0, 4, 4, 1)])

    def _run(self, beat: float) -> tuple[float, int, int, int]:
        i = bisect.bisect_right([r[0] for r in self.runs], beat + TOL) - 1
        return self.runs[max(0, i)]

    @staticmethod
    def bar_length(num: int, den: int) -> float:
        return num * 4.0 / den

    def position(self, beat: float) -> tuple[float, float, float]:
        """(position inside the bar, bar length, beat unit) at ``beat``."""
        start, num, den, _ = self._run(beat)
        length = self.bar_length(num, den)
        pos = (beat - start) % length
        if length - pos < TOL:
            pos = 0.0
        return pos, length, beat_unit(num, den)

    def is_downbeat(self, beat: float) -> bool:
        return self.position(beat)[0] < TOL

    def metric_class(self, beat: float) -> int:
        """0 downbeat, 1 on a beat of the meter, 2 on an eighth, 3 anything else."""
        pos, _, unit = self.position(beat)
        if pos < TOL:
            return 0
        if _multiple(pos, unit):
            return 1
        if _multiple(pos, 0.5):
            return 2
        return 3

    def bars(self) -> list[tuple[float, float, bool]]:
        """``(start, bar length, full)`` of every bar. A bar cut short by the next run's start
        (a pickup, or a meter arriving mid-bar) is not full."""
        out: list[tuple[float, float, bool]] = []
        for i, (start, num, den, n) in enumerate(self.runs):
            length = self.bar_length(num, den)
            nxt = self.runs[i + 1][0] if i + 1 < len(self.runs) else math.inf
            for k in range(n):
                b = start + k * length
                if b >= nxt - TOL:
                    break
                out.append((b, length, b + length <= nxt + TOL))
        return out

    def beats_per_bar(self) -> float:
        """Quarter-note beats per bar of the dominant meter (the one covering most full bars)."""
        weight: dict[float, int] = {}
        for _, length, full in self.bars():
            if full:
                weight[length] = weight.get(length, 0) + 1
        if not weight:
            for _, num, den, bars in self.runs:
                key = self.bar_length(num, den)
                weight[key] = weight.get(key, 0) + bars
        return max(weight.items(), key=lambda kv: (kv[1], kv[0]))[0]  # ties: the longer bar

    def downbeat_phase(self) -> float | None:
        """Phase in ``[0, beats_per_bar)`` of the dominant meter's bar lines: the phase shared by
        the most full bars of the dominant length (ties: the one reached first)."""
        bpb = self.beats_per_bar()
        clusters: list[list[float]] = []   # [phase, count, first start]
        for b, length, full in self.bars():
            if not full or abs(length - bpb) > TOL:
                continue
            ph = b % bpb
            if bpb - ph < TOL:
                ph = 0.0
            for c in clusters:
                d = abs(c[0] - ph)
                if min(d, bpb - d) < 2 * TOL:
                    c[1] += 1
                    break
            else:
                clusters.append([ph, 1, b])
        if not clusters:
            return None
        return max(clusters, key=lambda c: (c[1], -c[2]))[0]

    def first_downbeat(self, first_note: float) -> float:
        """First bar line of the dominant meter's grid for the music starting at ``first_note``:
        the last ``phase + k * beats_per_bar`` (k >= 0) at or before it, else the grid's first
        line. Consumers treat ``first_downbeat + k * beats_per_bar`` as the song's bar lines
        (influence excerpt snapping, the viewer's hand-off), so a pickup or odd-length first bar
        must not set the phase."""
        ph = self.downbeat_phase()
        if ph is None:
            return self.bar_start(first_note)
        if first_note + TOL < ph:
            return round(ph, 6)
        bpb = self.beats_per_bar()
        k = math.floor((first_note - ph + TOL) / bpb)
        return round(ph + k * bpb, 6)

    def bar_start(self, beat: float) -> float:
        """Start of the bar containing ``beat``."""
        start, num, den, _ = self._run(beat)
        length = self.bar_length(num, den)
        k = math.floor((beat - start + TOL) / length)
        return round(start + k * length, 6)


def _multiple(x: float, unit: float) -> bool:
    r = x % unit
    return r < TOL or unit - r < TOL
