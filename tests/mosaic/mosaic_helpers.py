"""Synthetic melodies for the mosaic tests."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.mosaic.match import Seq  # noqa: E402


def melody(pitches, durations, *, start: float = 0.0, gap: float = 0.0, work_id: str = "T", ibi: float = 0.5) -> Seq:
    """Notes one after another: ``durations`` in beats, each note ``gap`` beats shorter than its slot."""
    on = start + np.r_[0.0, np.cumsum(durations)[:-1]]
    off = on + np.asarray(durations, dtype=float) - gap
    return Seq(work_id, on, off, pitches, ibi)


def transform(seq: Seq, *, fold: float = 1.0, offset: float = 0.0, transpose: int = 0, work_id: str = "S",
              ibi: float | None = None) -> Seq:
    """The source a target was made from: target beat = fold * source beat + offset, target
    pitch = source pitch + transpose (so the source is the inverse)."""
    return Seq(work_id, (seq.on - offset) / fold, (seq.off - offset) / fold, seq.pitch - transpose,
               ibi if ibi is not None else seq.ibi)


RNG = np.random.default_rng(7)
TUNE = [60, 62, 64, 65, 67, 65, 64, 62, 60, 64, 67, 72, 71, 69, 67, 65]
RHYTHM = [1.0, 0.5, 0.5, 1.0, 1.0, 0.5, 0.5, 1.0, 0.75, 0.25, 1.0, 1.0, 0.5, 0.5, 1.0, 1.0]
