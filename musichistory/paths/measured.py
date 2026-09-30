"""Measured key and tempo of every previewed song (``audio.analysis.json`` + the MIDI prior),
and their agreement with the MIDI analysis."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from ..identity.key import key_name
from . import audio
from .graph import Graph
from .plan import wrap


@dataclass
class Measured:
    node_id: int
    work_id: str
    tonic: int
    mode: str
    key_source: str
    key_score: float
    key_margin: float
    key_prior_used: bool
    bpm: float
    bpm_source: str
    tempo_factor: float
    audio_bpm: float
    pulse: float
    midi_tonic: int
    midi_mode: str
    midi_bpm: float
    duration: float
    chords: list[int] = field(default_factory=list)
    bass: list[int] = field(default_factory=list)
    beat_chroma: str = ""
    grid_bpm: float = 0.0
    audio_tonic: int = 0
    audio_mode: str = "major"
    midi_gap: float = 0.0

    @property
    def key_name(self) -> str:
        return key_name(self.tonic, self.mode)

    @property
    def tonic_diff(self) -> int:
        """Audio tonic minus MIDI tonic, in [-6, 5] (0 = the MIDI is in the recording's key)."""
        return wrap(self.tonic - self.midi_tonic)

    @property
    def audio_tonic_diff(self) -> int:
        """The audio's own best key's tonic minus the MIDI tonic (before the prior)."""
        return wrap(self.audio_tonic - self.midi_tonic)

    @property
    def key_relation(self) -> str:
        return relation(self.tonic, self.mode, self.midi_tonic, self.midi_mode)

    @property
    def audio_key_relation(self) -> str:
        return relation(self.audio_tonic, self.audio_mode, self.midi_tonic, self.midi_mode)

    @property
    def tempo_ratio(self) -> float:
        return self.bpm / self.midi_bpm if self.midi_bpm > 0 else float("nan")


def relation(tonic: int, mode: str, midi_tonic: int, midi_mode: str) -> str:
    a = audio.key_index(tonic, mode)
    m = audio.key_index(midi_tonic, midi_mode)
    d = wrap(tonic - midi_tonic)
    if a == m:
        return "same"
    if audio.relative_of(a) == m:
        return "relative"
    if audio.parallel_of(a) == m:
        return "parallel"
    if mode == midi_mode and d in (5, -5):
        return "fifth"
    if mode == midi_mode:
        return "transposed"
    return "other"


def resolve_all(graph: Graph, analysis: dict) -> dict[int, Measured]:
    out = {}
    for nid, s in graph.songs.items():
        raw = analysis.get(s.work_id)
        if raw is None:
            continue
        k = audio.resolve_key(raw, s.tonic_pc, s.mode)
        t = audio.resolve_tempo(raw["tempo"], s.native_bpm)
        g = raw.get("grid") or {}
        out[nid] = Measured(nid, s.work_id, k.tonic, k.mode, k.source, k.score, k.margin, k.prior_used,
                            t.bpm, t.source, t.factor, t.audio_bpm, t.strength, s.tonic_pc, s.mode, s.native_bpm,
                            float(raw.get("duration") or 0.0), list(g.get("chords") or []), list(g.get("bass") or []),
                            g.get("chroma") or "", float(g.get("bpm") or 0.0), k.audio_tonic, k.audio_mode,
                            k.midi_gap)
    return out


def _tempo_class(r: float) -> str:
    if not r or math.isnan(r):
        return "n/a"
    for name, target in (("same", 1.0), ("double", 2.0), ("half", 0.5), ("3:2", 1.5), ("2:3", 2 / 3)):
        if abs(math.log(r / target)) <= 0.04:
            return name
    return "other"


def _buckets(ratios: list[float]) -> dict:
    """Songs by |audio/MIDI tempo ratio| distance from 1 (after the octave was resolved)."""
    edges = ((0.04, "<=4%"), (0.09, "4-9%"), (0.15, "9-15%"), (0.35, "15-35%"), (9e9, ">35%"))
    out = {name: 0 for _, name in edges}
    for r in ratios:
        if not r or math.isnan(r):
            continue
        d = abs(math.log(r))
        out[next(name for lim, name in edges if d <= lim)] += 1
    return out


def agreement(measured: dict[int, Measured]) -> dict:
    """Distributions and agreement rates with the MIDI analysis."""
    ms = list(measured.values())
    n = len(ms)
    if not n:
        return {"songs": 0}
    bpms = np.array([m.bpm for m in ms])
    ratio = [m.tempo_ratio for m in ms]
    rel = Counter(m.key_relation for m in ms)
    arel = Counter(m.audio_key_relation for m in ms)
    diff = Counter(m.audio_tonic_diff for m in ms)
    tclass = Counter(_tempo_class(r) for r in ratio)
    return {
        "songs": n,
        "key_source": dict(Counter(m.key_source for m in ms)),
        "bpm_source": dict(Counter(m.bpm_source for m in ms)),
        "mode": dict(Counter(m.mode for m in ms)),
        "key_prior_used": sum(1 for m in ms if m.key_prior_used),
        "audio_key_relation_to_midi": dict(arel.most_common()),
        "audio_key_exact_agreement": round(arel["same"] / n, 3),
        "audio_tonic_agreement": round(sum(1 for m in ms if m.audio_tonic_diff == 0) / n, 3),
        "audio_tonic_difference_histogram": {str(k): diff[k] for k in sorted(diff)},
        "final_key_relation_to_midi": dict(rel.most_common()),
        "bpm_percentiles": {str(p): round(float(np.percentile(bpms, p)), 1) for p in (0, 10, 25, 50, 75, 90, 100)},
        "tempo_ratio_to_midi": dict(tclass.most_common()),
        "tempo_agreement_4pct": round(tclass["same"] / n, 3),
        "tempo_ratio_buckets": _buckets(ratio),
        "tempo_octave_factor": dict(Counter(str(m.tempo_factor) for m in ms)),
        "key_margin_percentiles": {str(p): round(float(np.percentile([m.key_margin for m in ms], p)), 3)
                                   for p in (10, 50, 90)},
    }
