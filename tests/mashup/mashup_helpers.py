"""Synthetic songs for the mashup tests: a beat grid, chroma that spells a chord sequence
(one chord per half bar), vocal activity per bar."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.mashup.tracks import Track  # noqa: E402

NAMES = {"C": 0, "Db": 1, "D": 2, "Eb": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "Ab": 8, "A": 9, "Bb": 10, "B": 11}


def label(name: str) -> int:
    """'Am' -> 19, 'F' -> 10."""
    minor = name.endswith("m")
    return NAMES[name[:-1] if minor else name] * 2 + (1 if minor else 0)


def chord_chroma(lab: int) -> np.ndarray:
    r, minor = divmod(lab, 2)
    v = np.full(12, 0.05)
    for k in (0, 3 if minor else 4, 7):
        v[(r + k) % 12] = 1.0
    return v


def make_track(half_bar_chords: list[str], *, bpm: float = 120.0, bpb: int = 4, vocal: list[float] | None = None,
               key: tuple[int, str] = (0, "major"), work_id: str = "Q1", offset: float = 0.3, jitter: float = 0.0,
               transpose: int = 0, seed: int = 0) -> Track:
    """A song whose bar k carries chords half_bar_chords[2k], [2k+1] (transposed by ``transpose``)."""
    n_bars = len(half_bar_chords) // 2
    ibi = 60.0 / bpm
    rng = np.random.default_rng(seed)
    beats = offset + np.arange(n_bars * bpb + 1) * ibi + (rng.normal(0, jitter, n_bars * bpb + 1) if jitter else 0)
    beats = np.sort(beats)
    cols = []
    for k in range(n_bars):
        for h in range(2):
            lab = label(half_bar_chords[2 * k + h])
            r, m = divmod(lab, 2)
            lab = ((r + transpose) % 12) * 2 + m
            for _ in range(bpb // 2):
                cols.append(chord_chroma(lab))
    cols.append(cols[-1])
    chroma = np.stack(cols, axis=1)
    voc = np.repeat(np.asarray(vocal if vocal is not None else [1.0] * n_bars, dtype=float), bpb)
    voc = np.r_[voc, 0.0]
    return Track(work_id=work_id, tonic=key[0], mode=key[1], beats=beats, bpb=bpb, phase=0, chroma=chroma, vocal=voc,
                 duration=float(beats[-1] + 1.0), title=work_id, artist="test", year=2000)


AXIS = ["C", "C", "G", "G", "Am", "Am", "F", "F"]          # I | V | vi | IV, one chord per bar
