"""Measurements on a rendered mosaic (no planned value is trusted).

* ``heard_match``: the mosaic section's pieces bus (the pieces' vocals as rendered, before
  mixing) is transcribed again exactly as the previews were (pYIN, onsets, ``notes.segment``,
  the target's tuning), mapped to loop beats through the mix grid, and every loop of it is
  matched note for note against the target loop's melody (onset within ``match.ONSET_TOL``,
  durations similar; ``octave`` counts a note an octave off as heard right, since pieces may be
  folded an octave). Reported overall, per loop, per piece and inside the pieces' spans (and
  ``onset_pitch``: onset and pitch only, durations not compared); ``baseline`` is
  the same measurement on the target's own vocal in the first original loop - how much of the
  melody the transcription itself recovers.
* ``harmony``: each harmony voice's bus over the first harmony loop, transcribed the same way,
  against the target melody: the interval score (``harmony.consonance_of``).
* ``clicks`` (``mashup.verify.clicks``) at every join: loop boundaries, section changes and
  every piece cut of every mosaic loop; ``peak`` of the decoded MP3; ``beats``: beat_this on
  the decoded mix against the planned beat grid.

Only pitches and times: nothing is transcribed to words.
"""

from __future__ import annotations

import warnings

import numpy as np

from ..mashup import analysis as man
from ..mashup import verify as mverify
from . import harmony, match
from .notes import segment, vocal_onsets
from .render import Rendered

SR_P = 22050
HOP_P = 256


def transcribe(y: np.ndarray, sr: int, tuning: float = 0.0) -> list[tuple[float, float, int, float, float]]:
    """Notes (start s, end s, MIDI pitch, stability, unrounded pitch) of a rendered vocal signal."""
    import librosa

    m = y.mean(axis=1) if y.ndim == 2 else y
    if sr != SR_P:
        m = librosa.resample(m.astype(np.float32), orig_sr=sr, target_sr=SR_P)
    if not len(m) or float(np.max(np.abs(m))) < 1e-6:
        return []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        f0, voiced, _ = librosa.pyin(m, fmin=man.FMIN, fmax=man.FMAX, sr=SR_P, frame_length=2048, hop_length=HOP_P)
    midi = np.where(voiced & np.isfinite(f0), librosa.hz_to_midi(np.where(np.isfinite(f0), f0, 1.0)), np.nan)
    return segment(midi, HOP_P / SR_P, vocal_onsets(m, SR_P), tuning=tuning)


def to_loop_seq(notes, r: Rendered, min_stability: float = 0.5) -> match.Seq:
    """Notes transcribed from one loop of the mix (times from the loop's start), in loop beats."""
    rows = [(a, b, p, f) for a, b, p, s, f in notes if s >= min_stability]
    if not rows:
        return match.Seq("heard", [], [], [])
    a = np.array([x[0] for x in rows])
    b = np.array([x[1] for x in rows])
    return match.Seq("heard", r.grid.beat(a), r.grid.beat(b), [x[2] for x in rows], fpitch=[x[3] for x in rows])


def note_match(target: match.Seq, heard: match.Seq, *, octave: bool, durations: bool = True) -> np.ndarray:
    """Per target note: heard right (onset within tolerance, the same pitch - unrounded pitches
    within ``match.PITCH_TOL``, or that many octaves apart with ``octave`` - and, with
    ``durations``, a similar duration)."""
    out = np.zeros(len(target), dtype=bool)
    used: set[int] = set()
    for i in range(len(target)):
        d = np.abs(heard.on - target.on[i])
        for j in np.argsort(d):
            if d[j] > match.ONSET_TOL:
                break
            if j in used:
                continue
            dp = heard.fpitch[j] - target.fpitch[i]
            if octave:
                dp -= 12.0 * round(dp / 12.0)
            same = abs(dp) <= match.PITCH_TOL
            if same and (not durations
                         or match.durations_similar(heard.off[j] - heard.on[j], target.off[i] - target.on[i])):
                out[i] = True
                used.add(int(j))
                break
    return out


def heard_match(r: Rendered, target: match.Seq, spans: list[tuple[float, float]], tuning: float,
                sr: int) -> dict:
    """The rendered mosaic against the target melody, loop by loop (see the module doc)."""
    T = r.grid.seconds
    _, m0, mn = next(s for s in r.sections if s[0] == "mosaic")
    inside = np.zeros(len(target), dtype=bool)
    masks = []
    for a, b in spans:
        m = (target.on >= a - 1e-6) & (target.on <= b + 1e-6)
        masks.append(m)
        inside |= m
    per, per_exact, per_in, per_loose, per_piece = [], [], [], [], []
    for k in range(m0, m0 + mn):
        a, b = int(round(k * T * sr)), int(round((k + 1) * T * sr))
        heard = to_loop_seq(transcribe(r.buses["pieces"][a:b], sr, tuning), r)
        ok = note_match(target, heard, octave=True)
        ex = note_match(target, heard, octave=False)
        loose = note_match(target, heard, octave=True, durations=False)
        per.append(float(ok.mean()) if len(ok) else 0.0)
        per_exact.append(float(ex.mean()) if len(ex) else 0.0)
        per_in.append(float(ok[inside].mean()) if inside.any() else 0.0)
        per_loose.append(float(loose.mean()) if len(loose) else 0.0)
        per_piece.append([float(ok[m].mean()) if m.any() else 0.0 for m in masks])
    _, o0, _ = next(s for s in r.sections if s[0] == "original")
    a, b = int(round(o0 * T * sr)), int(round((o0 + 1) * T * sr))
    base = note_match(target, to_loop_seq(transcribe(r.buses["target_vocal"][a:b], sr, tuning), r), octave=True)
    return {"octave": round(float(np.mean(per)), 4), "exact": round(float(np.mean(per_exact)), 4),
            "in_pieces": round(float(np.mean(per_in)), 4), "per_loop": [round(x, 4) for x in per],
            "onset_pitch": round(float(np.mean(per_loose)), 4),
            "pieces": [round(float(x), 4) for x in np.mean(np.array(per_piece), axis=0)] if per_piece else [],
            "baseline": round(float(base.mean()), 4) if len(base) else None,
            "relative_to_baseline": round(float(np.mean(per)) / float(base.mean()), 4) if base.any() else None}


def harmony_heard(r: Rendered, target: match.Seq, tuning: float, sr: int) -> list[dict]:
    T = r.grid.seconds
    _, h0, _ = next(s for s in r.sections if s[0] == "harmony")
    a, b = int(round(h0 * T * sr)), int(round((h0 + 1) * T * sr))
    out = []
    k = 0
    while f"harmony_{k}" in r.buses:
        heard = to_loop_seq(transcribe(r.buses[f"harmony_{k}"][a:b], sr, tuning), r)
        notes = [[float(x), float(y), int(p)] for x, y, p in zip(heard.on, heard.off, heard.pitch)]
        c = harmony.consonance_of(target, notes, r.grid.n_beats)
        out.append({"heard_consonance": None if c is None else round(c, 4), "heard_notes": len(notes)})
        k += 1
    return out


def clicks(r: Rendered, normalized: np.ndarray, bpb: int, sr: int) -> dict:
    grid = np.array([t for t, _ in _beats(r)])
    return mverify.clicks(normalized, sr, r.joins, grid, bpb)


def _beats(r: Rendered) -> list[tuple[float, float]]:
    from .render import mix_beats

    return mix_beats(r)


def beats(r: Rendered, dec: np.ndarray, sr: int) -> dict:
    """beat_this on the decoded mix against the planned beats; a tracker counting twice as fast
    (or half as fast) is checked against the half-beat grid (it still lands on it)."""
    detected, method = mverify.mix_beats(None, dec, sr)
    grid = np.array([t for t, _ in _beats(r)])
    res = mverify.beat_agreement(grid, detected, 0.0, r.seconds)
    planned = 60.0 / float(np.median(np.diff(grid)))
    if res.get("bpm_heard") and abs(np.log2(res["bpm_heard"] / planned) - 1.0) < 0.1:
        halves = np.sort(np.r_[grid, (grid[:-1] + grid[1:]) / 2])
        res = {**mverify.beat_agreement(halves, detected, 0.0, r.seconds), "counted": "double time"}
    res["tracker"] = method
    res["bpm_planned"] = round(planned, 2)
    return res
