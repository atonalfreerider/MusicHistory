"""Sung notes of every preview, on its beat grid (cached in ``data/audio/mosaics/notes/<work_id>.json``).

* **Pitch** is the mashup analysis' pYIN track of the Demucs vocal stem (``mashup.analysis``:
  65-1050 Hz, 11.6 ms hop, voiced frames only), less the recording's tuning deviation so every
  song sits on the same semitone grid. Nothing is recomputed that the analysis already holds.
* **Onsets** of the vocal stem (librosa onset detection, backtracked) split repeated notes that
  a legato voice sings without an unvoiced gap.
* **Segmentation** (``segment``): unvoiced flickers of up to ``GAP_FILL`` frames are bridged,
  the pitch is median-smoothed over ``SMOOTH`` frames inside each voiced run, and a run is cut
  into notes where the pitch leaves the current semitone by more than ``HYSTERESIS`` for at
  least ``MIN_SWITCH`` frames (vibrato and scoops stay inside one note) and at every onset at
  least ``MIN_NOTE`` from the note's ends. A note's pitch is the rounded median of its frames,
  its ``stability`` the share of its frames within half a semitone of it (``fpitch``: the
  unrounded median, for comparisons that do not flip at a rounding boundary); notes shorter than
  ``MIN_NOTE`` are dropped, and a short unsteady note (``SCOOP``, ``SCOOP_STABILITY``) running
  straight into the next is a scoop: the next note starts there instead.
* **Beats**: start and end are also given in (fractional) beats of the preview's beat_this grid
  (``mashup.analysis``), linearly extrapolated by one beat at the ends; notes outside the grid
  are dropped. ``SongNotes.regrid`` recounts them on a grid of twice or half as many beats.

Only pitches and times: the vocal is never transcribed to words.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from .. import config
from ..mashup import stems as stems_mod

VERSION = 2
GAP_FILL = 2                 # unvoiced frames bridged inside a sung run
SMOOTH = 5                   # median filter length (frames) of the pitch inside a voiced run
HYSTERESIS = 0.7             # semitones away from the current note that start a new one ...
MIN_SWITCH = 5               # ... when held this many frames (58 ms)
MIN_NOTE = 0.09              # seconds: shorter notes are dropped, onsets closer to a note end ignored
SCOOP = 0.16                 # a note shorter than this ...
SCOOP_STABILITY = 0.7        # ... and this unsteady is a glide: absorbed by the next note, else dropped
ONSET_SR = 22050
ONSET_HOP = 256
COLS = ("t0", "t1", "b0", "b1", "pitch", "stability", "fpitch")


def notes_dir() -> Path:
    return config.DATA / "audio" / "mosaics" / "notes"


# --------------------------------------------------------------------------- segmentation
def _median_smooth(x: np.ndarray, k: int) -> np.ndarray:
    if len(x) < 3 or k < 3:
        return x
    h = k // 2
    pad = np.r_[np.full(h, x[0]), x, np.full(h, x[-1])]
    win = np.lib.stride_tricks.sliding_window_view(pad, k)
    return np.median(win, axis=1)


def voiced_runs(voiced: np.ndarray, gap_fill: int = GAP_FILL) -> list[tuple[int, int]]:
    """[start, end) frame runs of ``voiced`` with gaps of up to ``gap_fill`` frames bridged."""
    idx = np.flatnonzero(voiced)
    if not len(idx):
        return []
    runs = []
    a = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > gap_fill + 1:
            runs.append((a, prev + 1))
            a = i
        prev = i
    runs.append((a, prev + 1))
    return runs


def segment(pitch: np.ndarray, hop: float, onsets: Iterable[float] = (), *, tuning: float = 0.0
            ) -> list[tuple[float, float, int, float, float]]:
    """Notes (start s, end s, MIDI pitch, stability, unrounded median pitch) of a pitch track
    (fractional MIDI per frame, NaN unvoiced, frame i at ``i * hop`` s)."""
    p = np.asarray(pitch, dtype=float) - tuning
    voiced = np.isfinite(p)
    on = np.sort(np.asarray(list(onsets), dtype=float))
    min_frames = max(1, int(round(MIN_NOTE / hop)))
    out: list[tuple[float, float, int, float, float]] = []
    for a, b in voiced_runs(voiced):
        seg = p[a:b].copy()
        ok = np.isfinite(seg)
        if ok.sum() < min_frames:
            continue
        fill = np.flatnonzero(ok)
        seg = np.interp(np.arange(len(seg)), fill, seg[fill])       # bridge the flickers
        seg = _median_smooth(seg, SMOOTH)
        # hysteresis cut points
        cuts = [0]
        cur = round(float(np.median(seg[:min(len(seg), MIN_SWITCH * 2)])))
        i = 0
        while i < len(seg):
            if abs(seg[i] - cur) > HYSTERESIS:
                j = i
                while j < len(seg) and abs(seg[j] - cur) > HYSTERESIS and abs(seg[j] - seg[i]) <= 1.0:
                    j += 1
                if j - i >= MIN_SWITCH:
                    cuts.append(i)
                    cur = round(float(np.median(seg[i:j])))
                i = j if j > i else i + 1
            else:
                i += 1
        cuts.append(len(seg))
        # onset cut points
        t_run0 = a * hop
        for t in on[(on > t_run0 + MIN_NOTE) & (on < b * hop - MIN_NOTE)]:
            k = int(round((t - t_run0) / hop))
            if all(abs(k - c) >= min_frames for c in cuts):
                cuts.append(k)
        cuts = sorted(set(cuts))
        for c0, c1 in zip(cuts[:-1], cuts[1:]):
            if c1 - c0 < min_frames:
                continue
            raw = p[a + c0:a + c1]
            raw = raw[np.isfinite(raw)]
            if len(raw) < max(1, (c1 - c0) // 2):
                continue
            fm = float(np.median(raw))
            m = int(round(fm))
            stab = float(np.mean(np.abs(raw - m) <= 0.5))
            out.append(((a + c0) * hop, (a + c1) * hop, m, stab, fm))
    # a short unsteady note running straight into the next one is a scoop into it
    kept: list[tuple[float, float, int, float, float]] = []
    for k, n in enumerate(out):
        if n[1] - n[0] < SCOOP and n[3] < SCOOP_STABILITY:
            nxt = out[k + 1] if k + 1 < len(out) else None
            if nxt is not None and nxt[0] - n[1] <= 2 * hop and abs(nxt[2] - n[2]) <= 2:
                out[k + 1] = (n[0], *nxt[1:])
            continue
        kept.append(n)
    out = kept
    # merge same-pitch neighbours split only by the minimum-length rule (no onset between them)
    merged: list[tuple[float, float, int, float, float]] = []
    for n in out:
        if merged and merged[-1][2] == n[2] and n[0] - merged[-1][1] < 0.5 * hop \
                and not np.any((on > merged[-1][1] - MIN_NOTE) & (on < n[0] + MIN_NOTE)):
            q = merged[-1]
            wq, wn = q[1] - q[0], n[1] - n[0]
            merged[-1] = (q[0], n[1], q[2], (q[3] * wq + n[3] * wn) / (wq + wn), (q[4] * wq + n[4] * wn) / (wq + wn))
        else:
            merged.append(n)
    return merged


def to_beats(t, beats: np.ndarray):
    """Fractional beat index of seconds ``t`` on a beat grid (linear extrapolation at the ends)."""
    beats = np.asarray(beats, dtype=float)
    t = np.asarray(t, dtype=float)
    idx = np.arange(len(beats), dtype=float)
    out = np.interp(t, beats, idx)
    k0 = beats[1] - beats[0]
    k1 = beats[-1] - beats[-2]
    out = np.where(t < beats[0], (t - beats[0]) / k0, out)
    return np.where(t > beats[-1], len(beats) - 1 + (t - beats[-1]) / k1, out)


def to_seconds(q, beats: np.ndarray):
    """Seconds of fractional beat(s) ``q`` (the inverse of ``to_beats``)."""
    beats = np.asarray(beats, dtype=float)
    q = np.asarray(q, dtype=float)
    idx = np.arange(len(beats), dtype=float)
    out = np.interp(q, idx, beats)
    k0 = beats[1] - beats[0]
    k1 = beats[-1] - beats[-2]
    out = np.where(q < 0, beats[0] + q * k0, out)
    return np.where(q > len(beats) - 1, beats[-1] + (q - len(beats) + 1) * k1, out)


# --------------------------------------------------------------------------- onsets
def vocal_onsets(y: np.ndarray, sr: int) -> np.ndarray:
    """Backtracked onset times (s) of a vocal signal."""
    import librosa

    m = y.mean(axis=1) if y.ndim == 2 else y
    if sr != ONSET_SR:
        m = librosa.resample(m.astype(np.float32), orig_sr=sr, target_sr=ONSET_SR)
    env = librosa.onset.onset_strength(y=m, sr=ONSET_SR, hop_length=ONSET_HOP)
    return librosa.onset.onset_detect(onset_envelope=env, sr=ONSET_SR, hop_length=ONSET_HOP, units="time",
                                      backtrack=True, delta=0.1)


# --------------------------------------------------------------------------- per song
@dataclass
class SongNotes:
    work_id: str
    notes: np.ndarray                  # (n, 7): COLS
    beats: np.ndarray                  # the preview's beat times (s)
    tuning: float
    beats_per_bar: int
    downbeat_phase: int
    vocal_rel_db: float                # vocal stem RMS re the mix (dB)
    voiced_share: float                # voiced share of the preview
    duration: float
    meta: dict = field(default_factory=dict)

    factor: float = 1.0                # tempo-octave regrid applied (``regrid``)

    @property
    def ibi(self) -> float:
        d = np.diff(self.beats)
        return float(np.median(d)) if len(d) else 0.5

    def regrid(self, factor: float, phase: int | None = None) -> "SongNotes":
        """The same notes counted on a grid of twice (2) or half (0.5) as many beats, exactly as
        ``mashup.tracks.Track.regrid`` counts the song (half: every other beat from the
        downbeat at beat index ``phase``, by default the analysis' downbeat phase)."""
        ph = self.downbeat_phase if phase is None else int(phase)
        if factor == 1:
            return replace(self, downbeat_phase=ph)
        b = np.asarray(self.beats, dtype=float)
        if factor == 2:
            nb = np.empty(2 * len(b) - 1)
            nb[0::2], nb[1::2] = b, (b[:-1] + b[1:]) / 2
            phase = ph * 2
        elif factor == 0.5:
            start = ph % 2
            nb, phase = b[start::2], (ph - start) // 2
        else:
            raise ValueError(f"regrid factor must be 0.5, 1 or 2, not {factor}")
        rows = self.notes.copy()
        if len(rows) and len(nb) >= 2:
            rows[:, 2] = to_beats(rows[:, 0], nb)
            rows[:, 3] = to_beats(rows[:, 1], nb)
        return replace(self, notes=rows, beats=nb, downbeat_phase=int(phase), factor=self.factor * factor)

    @property
    def bpm(self) -> float:
        return 60.0 / self.ibi

    @property
    def pitch(self) -> np.ndarray:
        return self.notes[:, 4].astype(int)

    @property
    def b0(self) -> np.ndarray:
        return self.notes[:, 2]

    @property
    def b1(self) -> np.ndarray:
        return self.notes[:, 3]

    @property
    def fpitch(self) -> np.ndarray:
        return self.notes[:, 6]


def _from_doc(doc: dict) -> SongNotes:
    arr = np.asarray(doc["notes"], dtype=float).reshape(-1, len(COLS))
    return SongNotes(doc["work_id"], arr, np.asarray(doc["beats"], dtype=float), float(doc["tuning"]),
                     int(doc["beats_per_bar"]), int(doc["downbeat_phase"]), float(doc["vocal_rel_db"]),
                     float(doc["voiced_share"]), float(doc["duration"]))


def transcribe(work_id: str, *, force: bool = False) -> dict:
    """The cached note document of one separated, analyzed song (``mashup`` stage first)."""
    import soundfile

    from ..mashup import analysis

    out = stems_mod.stems_root() / work_id
    path = notes_dir() / f"{work_id}.json"
    an = json.loads(analysis.analysis_path(work_id).read_text(encoding="utf-8"))
    sha = an.get("source_sha256")
    if not force and path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("version") == VERSION and doc.get("source_sha256") == sha:
                return doc
        except ValueError:
            pass
    pitch = np.array([np.nan if v is None else float(v) for v in an["pitch"]], dtype=float)
    hop = float(an["pitch_hop"])
    y, sr = soundfile.read(str(out / "vocals.wav"), dtype="float32", always_2d=True)
    on = vocal_onsets(y, sr)
    tuning = float(an.get("tuning") or 0.0)
    raw = segment(pitch, hop, on, tuning=tuning)
    beats = np.asarray(an["beats"], dtype=float)
    rows = []
    if len(beats) >= 4:
        lo, hi = beats[0] - (beats[1] - beats[0]), beats[-1] + (beats[-1] - beats[-2])
        for t0, t1, m, stab, fm in raw:
            if t0 < lo or t1 > hi:
                continue
            q0, q1 = to_beats([t0, t1], beats)
            rows.append([round(t0, 4), round(t1, 4), round(float(q0), 4), round(float(q1), 4), int(m), round(stab, 3),
                         round(fm, 3)])
    lv = an.get("levels_db") or {}
    doc = {"version": VERSION, "source_sha256": sha, "work_id": work_id, "tuning": tuning,
           "beats": an["beats"], "beats_per_bar": int(an.get("beats_per_bar") or 4),
           "downbeat_phase": int(an.get("downbeat_phase") or 0),
           "vocal_rel_db": round(float(lv.get("vocals", -99.0)) - float(lv.get("mix", 0.0)), 2),
           "voiced_share": round(float(np.mean(np.isfinite(pitch))) if len(pitch) else 0.0, 4),
           "duration": float(an.get("duration") or 0.0), "onsets": len(on), "notes": rows}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return doc


def _job(work_id: str, force: bool) -> tuple[str, dict | None, str | None]:
    try:
        return work_id, transcribe(work_id, force=force), None
    except Exception as exc:  # one bad song must not stop the batch
        return work_id, None, f"{type(exc).__name__}: {exc}"


def load_all(work_ids: Iterable[str], *, force: bool = False, workers: int | None = None,
             log: Callable[[str], None] = print) -> tuple[dict[str, SongNotes], dict[str, str]]:
    """Notes of every song (transcribed in parallel where not cached). Returns (notes, failed)."""
    from ..mashup import analysis

    ids = sorted(set(work_ids))
    done: dict[str, SongNotes] = {}
    failed: dict[str, str] = {}
    todo = []
    for wid in ids:
        if not analysis.analysis_path(wid).is_file():
            failed[wid] = "no stems/analysis (run the mashup stage with --all)"
            continue
        p = notes_dir() / f"{wid}.json"
        if not force and p.is_file():
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                an_sha = json.loads(analysis.analysis_path(wid).read_text(encoding="utf-8")).get("source_sha256")
                if doc.get("version") == VERSION and doc.get("source_sha256") == an_sha:
                    done[wid] = _from_doc(doc)
                    continue
            except ValueError:
                pass
        todo.append(wid)
    if todo:
        workers = workers or max(1, min(4, os.cpu_count() or 2))
        log(f"  transcribing {len(todo)} vocal stems with {workers} workers ...")
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_job, wid, force) for wid in todo]
            for k, fut in enumerate(as_completed(futs), 1):
                wid, doc, err = fut.result()
                if doc is None:
                    failed[wid] = err or "failed"
                else:
                    done[wid] = _from_doc(doc)
                if k % 100 == 0:
                    log(f"    {k}/{len(todo)}")
    return done, failed
