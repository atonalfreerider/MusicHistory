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

    def beats_per_bar(self) -> float:
        """Quarter-note beats per bar of the dominant meter (the one covering most bars)."""
        weight: dict[float, int] = {}
        for _, num, den, bars in self.runs:
            key = self.bar_length(num, den)
            weight[key] = weight.get(key, 0) + bars
        return max(weight.items(), key=lambda kv: (kv[1], kv[0]))[0]  # ties: the longer bar

    def bar_start(self, beat: float) -> float:
        """Start of the bar containing ``beat``."""
        start, num, den, _ = self._run(beat)
        length = self.bar_length(num, den)
        k = math.floor((beat - start + TOL) / length)
        return round(start + k * length, 6)


def _multiple(x: float, unit: float) -> bool:
    r = x % unit
    return r < TOL or unit - r < TOL
