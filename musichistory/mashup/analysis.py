"""Per-song analysis for the mashup chains (cached in ``data/audio/stems/<work_id>/analysis.json``).

* **Beats and downbeats** of the full mix with ``beat_this`` (Foscarin, Schlueter & Widmer,
  ISMIR 2024; checkpoint ``final0``, minimal post-processing), the same tracker Resonance-2's
  Song Workshop uses. The checkpoint is copied once from Resonance-2's model folder when it is
  there (read only), else downloaded into ``data/models/beat_this``. Fallback without
  ``beat_this``: librosa's beat tracker with the downbeat phase taken from the bass onsets.
  ``beats_per_bar`` is the most common count of beats between downbeats and the bar grid is
  regular (every ``beats_per_bar`` beats from the most common downbeat phase), so a missed
  downbeat cannot shift the bars.
* **Chords per beat** of the accompaniment (``bass`` + ``other`` stems: no vocal, no drums):
  beat-synchronous CQT chroma of the harmonic part matched against triad/seventh templates and
  Viterbi-smoothed (``paths.audio.chord_labels``); labels are ``root*2 + minor`` in the
  recording's pitch classes (C = 0), -1 for a silent beat. The bass pitch class per beat too.
* **Tuning**: the accompaniment's deviation from A440 in semitones (librosa
  ``estimate_tuning``, -0.5 .. 0.5); old records are often a quarter tone off.
* **Vocal melody**: pYIN on the vocal stem (65-1050 Hz, 11.6 ms hop), voiced frames only, as
  fractional MIDI pitch; **vocal activity** per beat: the vocal stem's RMS in dB relative to
  its loud level (95th percentile) and the voiced share of the beat.

Nothing here reads or produces words: the vocal is only measured as pitch and energy.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np

from .. import config
from ..paths.audio import chord_labels
from . import stems as stems_mod

VERSION = 4
SR = 22050
HOP = 512
PITCH_HOP = 256
FMIN, FMAX = 65.0, 1050.0
ACTIVE_DB = -18.0                  # a beat is vocal-active above this level (dB re loud level)


# --------------------------------------------------------------------------- beats
def beat_checkpoint() -> str:
    """Local path of beat_this' final0 checkpoint (copied from Resonance-2 when present)."""
    dst = config.DATA / "models" / "beat_this" / "beat_this-final0.ckpt"
    if dst.is_file():
        return str(dst)
    src = config.RESONANCE_ROOT / "SongLibraryData" / "models" / "torch" / "hub" / "checkpoints" / "beat_this-final0.ckpt"
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return str(dst)
    os.environ.setdefault("TORCH_HOME", str(config.DATA / "models" / "torch"))
    return "final0"


_TRACKER = None


def track_beats(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, str]:
    """(beats, downbeats, method) in seconds for a mono signal."""
    global _TRACKER
    try:
        import torch
        from beat_this.inference import Audio2Beats
    except ImportError:
        return (*_librosa_beats(y, sr), "librosa")
    if _TRACKER is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        _TRACKER = Audio2Beats(checkpoint_path=beat_checkpoint(), device=device, dbn=False)
    beats, downbeats = _TRACKER(np.asarray(y, dtype=np.float32), sr)
    return np.asarray(beats, dtype=float), np.asarray(downbeats, dtype=float), "beat_this final0"


def _librosa_beats(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    import librosa

    _, frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=HOP)
    beats = librosa.frames_to_time(frames, sr=sr, hop_length=HOP)
    low = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP, fmax=200)
    strength = [float(low[min(int(f), len(low) - 1)]) for f in frames]
    phase = int(np.argmax([np.mean(strength[p::4]) if strength[p::4] else 0 for p in range(4)]))
    return beats, beats[phase::4]


def bar_grid(beats: np.ndarray, downbeats: np.ndarray) -> tuple[int, int]:
    """(beats_per_bar, phase): the most common beat count between downbeats (2..7, default 4)
    and the most common index (mod beats_per_bar) of a downbeat among the beats."""
    beats = np.asarray(beats, dtype=float)
    if len(beats) < 4 or len(downbeats) < 2:
        return 4, 0
    idx = [int(np.argmin(np.abs(beats - d))) for d in downbeats]
    idx = [i for i, d in zip(idx, downbeats) if abs(beats[i] - d) < 0.07]
    counts = [b - a for a, b in zip(idx, idx[1:]) if 2 <= b - a <= 7]
    bpb = int(np.bincount(counts).argmax()) if counts else 4
    phases = np.bincount([i % bpb for i in idx], minlength=bpb) if idx else np.zeros(bpb)
    return bpb, int(np.argmax(phases))


# --------------------------------------------------------------------------- packing
def pack_u8(a: np.ndarray) -> str:
    """12 x n non-negative -> base64 uint8, each column scaled to its max."""
    a = np.asarray(a, dtype=float)
    if a.size == 0:
        return ""
    m = np.maximum(a.max(axis=0, keepdims=True), 1e-9)
    return base64.b64encode(np.round(255 * a / m).astype(np.uint8).tobytes(order="F")).decode("ascii")


def unpack_u8(s: str, rows: int = 12) -> np.ndarray:
    if not s:
        return np.zeros((rows, 0))
    raw = np.frombuffer(base64.b64decode(s), dtype=np.uint8).astype(float) / 255.0
    return raw.reshape((rows, -1), order="F")


# --------------------------------------------------------------------------- measure
def _mono(y: np.ndarray) -> np.ndarray:
    return y.mean(axis=1) if y.ndim == 2 else y


def _resample(y: np.ndarray, sr: int, target: int = SR) -> np.ndarray:
    import librosa

    return librosa.resample(y, orig_sr=sr, target_sr=target) if sr != target else y


def measure(mix: np.ndarray, vocals: np.ndarray, accomp: np.ndarray, sr: int) -> dict:
    """Analysis of one song from its mix and stems (arrays (n,) or (n, ch) at ``sr``)."""
    import warnings

    import librosa

    m = _resample(_mono(mix).astype(np.float32), sr)
    v = _resample(_mono(vocals).astype(np.float32), sr)
    a = _resample(_mono(accomp).astype(np.float32), sr)
    n = min(len(m), len(v), len(a))
    m, v, a = m[:n], v[:n], a[:n]
    beats, downbeats, method = track_beats(m, SR)
    beats = beats[beats < n / SR - 0.05]
    bpb, phase = bar_grid(beats, downbeats)

    # Chords and bass per beat (accompaniment without drums).
    a_h, _ = librosa.effects.hpss(a)
    tuning = float(librosa.estimate_tuning(y=a_h, sr=SR))
    chroma = librosa.feature.chroma_cqt(y=a_h, sr=SR, hop_length=HOP)
    bass = np.abs(librosa.cqt(a_h, sr=SR, hop_length=HOP, fmin=librosa.note_to_hz("C1"), n_bins=36))
    bass = bass.reshape(3, 12, -1).sum(axis=0)
    nf = min(chroma.shape[1], bass.shape[1])
    chroma, bass = chroma[:, :nf], bass[:, :nf]
    rms_a = librosa.feature.rms(y=a_h, hop_length=HOP)[0][:nf]
    floor = 0.1 * (float(np.median(rms_a)) + 1e-12)
    bf = np.clip(librosa.time_to_frames(beats, sr=SR, hop_length=HOP), 0, nf - 1)
    edges = list(bf) + [nf]
    segs = [slice(int(x), int(max(y, x + 1))) for x, y in zip(edges[:-1], edges[1:])]
    if segs:
        bchroma = np.stack([np.median(chroma[:, s], axis=1) for s in segs], axis=1)
        bbass = np.stack([np.mean(bass[:, s], axis=1) for s in segs], axis=1)
        brms = np.array([float(np.mean(rms_a[s])) for s in segs])
        quiet = brms < floor
        bchroma[:, quiet] = 0.0
        chords = chord_labels(bchroma)
        bass_pc = [-1 if quiet[i] else int(np.argmax(bbass[:, i])) for i in range(len(segs))]
    else:
        bchroma, chords, bass_pc = np.zeros((12, 0)), [], []

    # Vocal pitch (pYIN) and activity per beat.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        f0, voiced, vprob = librosa.pyin(v, fmin=FMIN, fmax=FMAX, sr=SR, frame_length=2048, hop_length=PITCH_HOP)
    times = librosa.times_like(f0, sr=SR, hop_length=PITCH_HOP)
    midi = np.where(voiced & np.isfinite(f0), librosa.hz_to_midi(np.where(np.isfinite(f0), f0, 1.0)), np.nan)
    rms_v = librosa.feature.rms(y=v, frame_length=2048, hop_length=PITCH_HOP)[0][: len(times)]
    loud = float(np.percentile(rms_v, 95)) + 1e-9
    vol_db = 20 * np.log10(np.maximum(rms_v, 1e-9) / loud)
    bt = np.r_[beats, n / SR]
    act_db, voiced_share = [], []
    for i in range(len(beats)):
        sel = (times >= bt[i]) & (times < bt[i + 1])
        if not sel.any():
            act_db.append(-60.0)
            voiced_share.append(0.0)
            continue
        act_db.append(float(np.max(vol_db[sel])))
        voiced_share.append(float(np.mean(np.isfinite(midi[sel]))))
    rms_mix = float(np.sqrt(np.mean(m.astype(np.float64) ** 2)))
    rms_voc = float(np.sqrt(np.mean(v.astype(np.float64) ** 2)))
    rms_acc = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
    return {
        "version": VERSION, "duration": round(n / SR, 3), "beat_method": method,
        "beats": [round(float(t), 4) for t in beats], "downbeats": [round(float(t), 4) for t in downbeats],
        "beats_per_bar": bpb, "downbeat_phase": phase,
        "chords": [int(c) for c in chords], "bass": bass_pc, "beat_chroma": pack_u8(bchroma),
        "vocal_db": [round(x, 1) for x in act_db], "vocal_voiced": [round(x, 3) for x in voiced_share],
        "tuning": round(tuning, 3),
        "pitch_hop": PITCH_HOP / SR,
        "pitch": [None if not np.isfinite(p) else round(float(p), 2) for p in midi],
        "levels_db": {"mix": round(20 * np.log10(rms_mix + 1e-12), 2), "vocals": round(20 * np.log10(rms_voc + 1e-12), 2),
                      "accompaniment": round(20 * np.log10(rms_acc + 1e-12), 2)},
    }


def analysis_path(work_id: str) -> Path:
    return stems_mod.stems_root() / work_id / "analysis.json"


def analyze(work_id: str, preview: Path, *, force: bool = False) -> dict:
    """Cached analysis of one separated song (keyed by the separation's source SHA-256)."""
    out = stems_mod.stems_root() / work_id
    sep = json.loads((out / "separation.json").read_text(encoding="utf-8"))
    path = analysis_path(work_id)
    if not force and path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("version") == VERSION and doc.get("source_sha256") == sep.get("sourceSha256"):
                return doc
        except ValueError:
            pass
    import soundfile

    t0 = time.monotonic()
    mix, sr = soundfile.read(str(preview), dtype="float32", always_2d=True)
    voc, _ = stems_mod.read_stem(out, "vocals")
    bass, _ = stems_mod.read_stem(out, "bass")
    other, _ = stems_mod.read_stem(out, "other")
    doc = measure(mix, voc, bass + other, sr)
    doc["source_sha256"] = sep.get("sourceSha256")
    doc["seconds"] = round(time.monotonic() - t0, 1)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return doc


def lead_pitch(work_id: str, stem: str = "other", *, min_prob: float = 0.5) -> tuple[list, float]:
    """pYIN pitch track of an instrumental lead (a song without a sung vocal), cached in
    ``lead_pitch.json``: (frames as MIDI or None, hop seconds). Only confidently voiced frames
    (probability >= ``min_prob``) are kept, since the stem is polyphonic."""
    import warnings

    import librosa

    out = stems_mod.stems_root() / work_id
    path = out / f"lead_pitch_{stem}.json"
    sep = json.loads((out / "separation.json").read_text(encoding="utf-8"))
    if path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("version") == VERSION and doc.get("source_sha256") == sep.get("sourceSha256"):
                return doc["pitch"], doc["hop"]
        except ValueError:
            pass
    y, sr = stems_mod.read_stem(out, stem)
    y = _resample(_mono(y), sr)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        f0, voiced, prob = librosa.pyin(y, fmin=FMIN, fmax=FMAX, sr=SR, frame_length=2048, hop_length=PITCH_HOP)
    ok = voiced & np.isfinite(f0) & (prob >= min_prob)
    midi = np.where(ok, librosa.hz_to_midi(np.where(np.isfinite(f0), f0, 1.0)), np.nan)
    pitch = [None if not np.isfinite(p) else round(float(p), 2) for p in midi]
    doc = {"version": VERSION, "source_sha256": sep.get("sourceSha256"), "stem": stem, "hop": PITCH_HOP / SR,
           "pitch": pitch}
    path.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    return pitch, PITCH_HOP / SR
