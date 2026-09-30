"""Measure every recording preview: tempo, key, and a per-beat chord/bass reading.

The recording is the truth for audio; the song's MIDI analysis (song_node) is only a prior:

* **Tempo.** librosa's onset autocorrelation (tempogram) gives an audio-only estimate T0
  (librosa's default log-normal prior at 120 BPM). Its octave relatives T0/4 .. 4·T0 (40–260
  BPM) are the candidates; each is beat-tracked (``beat_track(bpm=c)``) and scored by the
  tempogram's strength at its lag. The MIDI ``native_bpm`` resolves the octave: the candidate
  nearest to it (log distance) wins unless its strength is under ``OCTAVE_MIN_STRENGTH`` of
  the strongest candidate's (then the next nearest). The BPM is the beat tracker's
  (inter-beat intervals counted in whole beats over the span, robust to a skipped or an extra
  beat) when it agrees with the candidate within 6 %, else the candidate's. Source ``midi``
  when the pulse is unclear (strength under ``MIN_PULSE``, fewer than ``MIN_BEATS`` beats or
  irregular beats).
* **Key.** Chroma (CQT of the harmonic part after HPSS, quiet frames dropped) correlated with
  the Temperley-Kostka-Payne and Krumhansl-Kessler profiles (``identity/key.py``, weighted
  0.25 : 0.10 as there), plus ``CHORD_WEIGHT`` × a chord-function score: the share of the
  clip's estimated triads weighted by their role in the key (tonic 1, dominant 0.7,
  subdominant 0.6, ..., chords outside the key −0.2). The MIDI key is a prior: it gets
  ``MIDI_PRIOR`` added to its score, so it stands whenever the audio's evidence against it is
  weak (typically a fifth, relative or parallel call, where chroma profiles are ambiguous) and
  the audio wins whenever it clearly hears another key (typically a transposed fan MIDI).
  Source ``audio`` when the audio's own best key is the result, ``midi`` when the prior
  decided or the chroma is flat (profile score under ``MIN_KEY_SCORE``). Tuned on 76
  recordings with documented keys (exact / MIREX-weighted): profiles alone 50 / 0.74, + chord
  function 54 / 0.78, the MIDI key alone 62 / 0.84, the relative/parallel-only prior 54-59,
  this rule 64 / 0.88; a bass chroma or a tonic-chord vote made it worse.
* **Chords.** On the beat grid of the candidate nearest 110 BPM: beat-synchronous chroma
  matched against major/minor triad (and seventh) templates, smoothed with a Viterbi pass
  (``CHANGE_PENALTY``), plus the strongest bass pitch class (C1–B3) per beat. Used to check
  that a shared identity is audible (``identity.py``).

Raw measurements (profile scores, chord histogram, tempo candidates, beat chords) do not
depend on the MIDI and are cached in ``data/audio/analysis.json`` keyed by the preview's size
and SHA-1, so reruns are instant; ``resolve_key``/``resolve_tempo`` apply the MIDI prior at
load time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..identity.key import KK_MAJOR, KK_MINOR, MAJOR, MINOR, TKP_MAJOR, TKP_MINOR, key_name

ANALYSIS_VERSION = 2
SR = 22050
HOP = 512
TKP_WEIGHT, KK_WEIGHT = 0.25, 0.10       # identity/key.py WEIGHTS
CHORD_WEIGHT = 1.0
MIDI_PRIOR = 0.4                          # added to the MIDI key's score (audio must beat it by more)
MIN_KEY_SCORE = 0.2
OCTAVE_MIN_STRENGTH = 0.5                 # a candidate needs this share of the strongest candidate's strength
MIN_PULSE = 0.08
MIN_BEATS = 12
MAX_IBI_CV = 0.25
BEAT_AGREE = 0.06                         # |log(beat bpm / candidate)| allowed before the candidate wins
TEMPO_RANGE = (40.0, 260.0)
CHORD_GRID_BPM = 110.0
CHANGE_PENALTY = 0.12
KEY_FRAME_STEP = 4                        # chord histogram for the key: one chord every 4 frames (~93 ms)

# Weight of a chord (degree above the tonic, minor?) in a major / minor key; others -0.2.
MAJOR_ROLES = {(0, 0): 1.0, (7, 0): 0.7, (5, 0): 0.6, (9, 1): 0.45, (2, 1): 0.4, (4, 1): 0.3, (10, 0): 0.3,
               (5, 1): 0.2, (8, 0): 0.15, (3, 0): 0.1}
MINOR_ROLES = {(0, 1): 1.0, (5, 1): 0.6, (7, 0): 0.6, (8, 0): 0.5, (3, 0): 0.5, (10, 0): 0.5, (7, 1): 0.4,
               (5, 0): 0.2, (2, 1): 0.1}
OUTSIDE = -0.2


# --------------------------------------------------------------------------- keys
def key_index(tonic: int, mode: str) -> int:
    """0..11 major keys, 12..23 minor keys."""
    return tonic % 12 + (12 if mode == MINOR else 0)


def index_key(i: int) -> tuple[int, str]:
    return i % 12, (MINOR if i >= 12 else MAJOR)


def _rotations(major: list[float], minor: list[float]) -> np.ndarray:
    """24 x 12 matrix of the profiles rotated to every tonic, each row standardized."""
    rows = [np.roll(np.array(prof, dtype=float), t) for prof in (major, minor) for t in range(12)]
    m = np.array(rows)
    m = m - m.mean(axis=1, keepdims=True)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


_TKP = _rotations(TKP_MAJOR, TKP_MINOR)
_KK = _rotations(KK_MAJOR, KK_MINOR)


def _corr_all(hist: np.ndarray, rotations: np.ndarray) -> np.ndarray:
    """Pearson correlation of a 12-bin histogram with every rotated profile (0 if flat)."""
    h = np.asarray(hist, dtype=float)
    if h.sum() <= 0 or np.allclose(h, h[0]):
        return np.zeros(24)
    h = h - h.mean()
    return rotations @ (h / np.linalg.norm(h))


def profile_scores(hist: Iterable[float]) -> np.ndarray:
    """Weighted TKP/KK correlation of a 12-bin pitch-class histogram with all 24 keys."""
    h = np.asarray(list(hist), dtype=float)
    return (TKP_WEIGHT * _corr_all(h, _TKP) + KK_WEIGHT * _corr_all(h, _KK)) / (TKP_WEIGHT + KK_WEIGHT)


def chord_scores(chord_hist: Iterable[float]) -> np.ndarray:
    """Chord-function score of all 24 keys from a 24-bin chord histogram (label root*2 + minor)."""
    ch = np.asarray(list(chord_hist), dtype=float)
    total = ch.sum()
    out = np.zeros(24)
    if total <= 0:
        return out
    for k in range(24):
        tonic, mode = index_key(k)
        roles = MINOR_ROLES if mode == MINOR else MAJOR_ROLES
        out[k] = sum(roles.get(((lab // 2 - tonic) % 12, lab % 2), OUTSIDE) * ch[lab] for lab in range(24) if ch[lab]) / total
    return out


def key_scores(hist: Iterable[float], chord_hist: Iterable[float] | None = None,
               profile: np.ndarray | None = None) -> np.ndarray:
    s = profile_scores(hist) if profile is None else profile
    if chord_hist is not None:
        s = s + CHORD_WEIGHT * chord_scores(chord_hist)
    return s


def relative_of(i: int) -> int:
    t, m = index_key(i)
    return key_index((t + 9) % 12, MINOR) if m == MAJOR else key_index((t + 3) % 12, MAJOR)


def parallel_of(i: int) -> int:
    t, m = index_key(i)
    return key_index(t, MINOR if m == MAJOR else MAJOR)


@dataclass
class KeyResult:
    tonic: int
    mode: str
    score: float           # audio score of the chosen key
    margin: float          # audio: best key minus runner-up (confidence of the audio alone)
    source: str            # 'audio' (the audio's own best key) | 'midi' (the prior decided, or flat chroma)
    prior_used: bool       # the MIDI prior changed the audio's own best key
    audio_tonic: int       # the audio's own best key
    audio_mode: str
    midi_gap: float = 0.0  # audio best minus the MIDI key's audio score (evidence against the MIDI key)

    @property
    def name(self) -> str:
        return key_name(self.tonic, self.mode)


def resolve_key(raw: dict, midi_tonic: int | None, midi_mode: str | None) -> KeyResult:
    """Key of a measured preview (``raw`` = its analysis entry) with the MIDI key as prior."""
    prof = profile_scores(raw["chroma_hist"])
    s = key_scores(raw["chroma_hist"], raw.get("chord_hist"), prof)
    order = np.argsort(s)[::-1]
    best = int(order[0])
    bt, bm = index_key(best)
    margin = round(float(s[best] - s[int(order[1])]), 4)
    if midi_tonic is None:
        return KeyResult(bt, bm, round(float(s[best]), 4), margin, "audio", False, bt, bm)
    midi = key_index(int(midi_tonic), midi_mode or MAJOR)
    gap = round(float(s[best] - s[midi]), 4)
    if prof.max() < MIN_KEY_SCORE:
        return KeyResult(int(midi_tonic) % 12, midi_mode or MAJOR, round(float(s[midi]), 4), margin, "midi",
                         midi != best, bt, bm, gap)
    choice = midi if gap < MIDI_PRIOR else best
    t, m = index_key(choice)
    return KeyResult(t, m, round(float(s[choice]), 4), margin, "audio" if choice == best else "midi",
                     choice != best, bt, bm, gap)


# --------------------------------------------------------------------------- tempo
@dataclass
class TempoResult:
    bpm: float
    source: str            # 'audio' | 'midi'
    factor: float          # chosen candidate / audio-only estimate
    audio_bpm: float       # librosa's audio-only estimate (T0), refined by its beats
    strength: float
    n_beats: int


def _log2(x: float) -> float:
    return math.log2(x) if x > 0 else float("inf")


def _refined(c: dict) -> float:
    b = c.get("beat_bpm")
    if b and abs(math.log(b / c["bpm"])) <= BEAT_AGREE:
        return float(b)
    return float(c["bpm"])


def resolve_tempo(raw: dict, midi_bpm: float | None) -> TempoResult:
    """Pick the octave candidate (``raw['candidates']``) with the MIDI tempo as prior."""
    cands = [c for c in raw.get("candidates") or [] if c.get("bpm")]
    if not cands:
        return TempoResult(float(midi_bpm or 120.0), "midi", 1.0, 0.0, 0.0, 0)
    strongest = max(c["strength"] for c in cands)
    base = next((c for c in cands if abs(c["factor"] - 1.0) < 1e-9), cands[0])
    audio_bpm = _refined(base)
    if midi_bpm and midi_bpm > 0:
        order = sorted(cands, key=lambda c: abs(_log2(c["bpm"] / midi_bpm)))
        chosen = next((c for c in order if c["strength"] >= OCTAVE_MIN_STRENGTH * strongest - 1e-12), order[0])
    else:
        chosen = base
    bpm = _refined(chosen)
    cv = chosen.get("ibi_cv")
    clear = (chosen["strength"] >= MIN_PULSE and (chosen.get("n_beats") or 0) >= MIN_BEATS
             and (cv if cv is not None else 1.0) <= MAX_IBI_CV)
    if not clear and midi_bpm and midi_bpm > 0:
        return TempoResult(float(midi_bpm), "midi", chosen["factor"], round(audio_bpm, 2), chosen["strength"],
                           int(chosen.get("n_beats") or 0))
    return TempoResult(round(bpm, 2), "audio", chosen["factor"], round(audio_bpm, 2), chosen["strength"],
                       int(chosen.get("n_beats") or 0))


def beat_bpm(beat_times: Iterable[float]) -> tuple[float | None, float | None]:
    """(BPM from beats counted in whole inter-beat intervals over the span, IBI coefficient of
    variation). A skipped beat counts as two intervals and an extra beat's two halves as one,
    so neither reads as a tempo change."""
    t = np.asarray(list(beat_times), dtype=float)
    if len(t) < 4:
        return None, None
    ibi = np.diff(t)
    med = float(np.median(ibi))
    if med <= 0:
        return None, None
    counts = np.round(ibi / med)
    span = float(t[-1] - t[0])
    total = float(counts.sum())
    if span <= 0 or total < 1:
        return None, None
    whole = counts >= 1
    norm = ibi[whole] / counts[whole]
    cv = float(np.std(norm) / np.mean(norm)) if len(norm) and np.mean(norm) > 0 else None
    return 60.0 * total / span, cv


# --------------------------------------------------------------------------- chords
def _templates() -> tuple[np.ndarray, list[int]]:
    """Unit templates and the chord label (root*2 + minor) each stands for."""
    shapes = [((0, 4, 7), 0), ((0, 3, 7), 1), ((0, 4, 7, 10), 0), ((0, 3, 7, 10), 1)]
    rows, labels = [], []
    for notes, minor in shapes:
        for root in range(12):
            v = np.zeros(12)
            for k, n in enumerate(notes):
                v[(root + n) % 12] = 1.0 if k < 3 else 0.6
            rows.append(v / np.linalg.norm(v))
            labels.append(root * 2 + minor)
    return np.array(rows), labels


TEMPLATES, TEMPLATE_LABELS = _templates()


def chord_labels(chroma: np.ndarray, penalty: float = CHANGE_PENALTY) -> list[int]:
    """One chord per column (root*2 + minor; -1 for a silent column) of a 12 x n chroma,
    matched against triad/seventh templates and Viterbi-smoothed (a change costs ``penalty``)."""
    X = np.asarray(chroma, dtype=float)
    if X.ndim != 2 or X.shape[1] == 0:
        return []
    norms = np.linalg.norm(X, axis=0)
    Xn = X / np.maximum(norms, 1e-9)
    sim_t = TEMPLATES @ Xn                                # (48, n)
    sim = np.full((24, X.shape[1]), -1.0)
    for row, lab in enumerate(TEMPLATE_LABELS):
        sim[lab] = np.maximum(sim[lab], sim_t[row])
    n = X.shape[1]
    dp = sim[:, 0].copy()
    back = np.zeros((24, n), dtype=int)
    for t in range(1, n):
        j = int(np.argmax(dp))
        switch = dp[j] - penalty
        take_switch = switch > dp
        back[:, t] = np.where(take_switch, j, np.arange(24))
        dp = np.where(take_switch, switch, dp) + sim[:, t]
    path = [int(np.argmax(dp))]
    for t in range(n - 1, 0, -1):
        path.append(int(back[path[-1], t]))
    path.reverse()
    quiet = norms < 1e-6
    return [-1 if quiet[i] else path[i] for i in range(n)]


# --------------------------------------------------------------------------- measurement
def _pack(arr: np.ndarray) -> str:
    """12 x n non-negative array -> base64 of uint8 (each column scaled to its max)."""
    a = np.asarray(arr, dtype=float)
    m = np.maximum(a.max(axis=0, keepdims=True), 1e-9)
    return base64.b64encode(np.round(255 * a / m).astype(np.uint8).tobytes(order="F")).decode("ascii")


def unpack(s: str) -> np.ndarray:
    if not s:
        return np.zeros((12, 0))
    raw = np.frombuffer(base64.b64decode(s), dtype=np.uint8).astype(float) / 255.0
    return raw.reshape((12, -1), order="F")


def measure(y: np.ndarray, sr: int = SR) -> dict:
    """Raw audio measurements of one preview (no MIDI involved)."""
    import librosa

    y = np.asarray(y, dtype=np.float32)
    duration = len(y) / sr
    y_h, _ = librosa.effects.hpss(y)
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tg = librosa.feature.tempogram(onset_envelope=oenv, sr=sr, hop_length=HOP)
    curve = tg.mean(axis=1)
    lags = np.arange(len(curve))

    def strength(bpm: float) -> float:
        lag = 60.0 * sr / (HOP * bpm)
        sel = (lags >= lag / 1.04) & (lags <= lag * 1.04)
        return float(curve[sel].max()) if sel.any() else float(np.interp(lag, lags, curve))

    t0 = float(np.atleast_1d(librosa.feature.tempo(onset_envelope=oenv, sr=sr, hop_length=HOP))[0])
    candidates, frames_of = [], {}
    for factor in (0.25, 0.5, 1.0, 2.0, 4.0):
        bpm = t0 * factor
        if factor != 1.0 and not (TEMPO_RANGE[0] <= bpm <= TEMPO_RANGE[1]):
            continue
        _, beats = librosa.beat.beat_track(onset_envelope=oenv, sr=sr, hop_length=HOP, bpm=bpm, units="frames")
        bb, cv = beat_bpm(librosa.frames_to_time(beats, sr=sr, hop_length=HOP))
        frames_of[factor] = np.asarray(beats, dtype=int)
        candidates.append(dict(factor=factor, bpm=round(bpm, 3), strength=round(strength(bpm), 4),
                               beat_bpm=round(bb, 3) if bb else None, ibi_cv=round(cv, 4) if cv is not None else None,
                               n_beats=int(len(beats))))

    # Key: harmonic chroma over the non-quiet frames + a coarse chord histogram.
    chroma = librosa.feature.chroma_cqt(y=y_h, sr=sr, hop_length=HOP)
    bass = np.abs(librosa.cqt(y_h, sr=sr, hop_length=HOP, fmin=librosa.note_to_hz("C1"), n_bins=36))
    bass = bass.reshape(3, 12, -1).sum(axis=0)
    n = min(chroma.shape[1], bass.shape[1])
    chroma, bass = chroma[:, :n], bass[:, :n]
    rms = librosa.feature.rms(y=y_h, hop_length=HOP)[0][:n]
    floor = 0.1 * (float(np.median(rms)) + 1e-12)
    keep = rms > floor
    if keep.sum() < 10:
        keep = np.ones(n, dtype=bool)
    hist = chroma[:, keep].mean(axis=1)
    hist = hist / max(float(hist.sum()), 1e-9)
    coarse = chord_labels(chroma[:, ::KEY_FRAME_STEP])
    chord_hist = np.bincount([c for c in coarse if c >= 0], minlength=24)

    # Chords and bass per beat on the grid nearest CHORD_GRID_BPM.
    grid = min(candidates, key=lambda c: abs(math.log(c["bpm"] / CHORD_GRID_BPM)))
    frames = np.asarray([f for f in frames_of[grid["factor"]] if f < n], dtype=int)
    chords: list[int] = []
    bass_pcs: list[int] = []
    beat_chroma = np.zeros((12, 0))
    if len(frames) >= 2:
        ends = list(frames[1:]) + [n]
        segs = [slice(int(a), int(max(b, a + 1))) for a, b in zip(frames, ends)]
        beat_chroma = librosa.util.sync(chroma, segs, aggregate=np.median, pad=False)
        beat_bass = librosa.util.sync(bass, segs, aggregate=np.mean, pad=False)
        beat_rms = librosa.util.sync(rms[None, :], segs, aggregate=np.mean, pad=False)[0]
        quiet = beat_rms < floor
        beat_chroma[:, quiet] = 0.0
        chords = chord_labels(beat_chroma)
        bass_pcs = [-1 if quiet[i] else int(np.argmax(beat_bass[:, i])) for i in range(beat_bass.shape[1])]
    beat_times = librosa.frames_to_time(frames, sr=sr, hop_length=HOP) if len(frames) else np.zeros(0)
    return dict(
        version=ANALYSIS_VERSION,
        duration=round(duration, 3),
        tempo=dict(audio_estimate=round(t0, 3), candidates=candidates),
        chroma_hist=[round(float(v), 5) for v in hist],
        chord_hist=[int(v) for v in chord_hist],
        grid=dict(bpm=round(float(grid["beat_bpm"] or grid["bpm"]), 3), factor=grid["factor"],
                  beats=[round(float(t), 3) for t in beat_times[: len(chords)]],
                  chords=chords, bass=bass_pcs, chroma=_pack(beat_chroma) if beat_chroma.shape[1] else ""),
    )


def load_audio(path: Path, sr: int = SR) -> np.ndarray:
    import librosa

    y, _ = librosa.load(str(path), sr=sr, mono=True)
    return y


def file_id(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return f"{path.stat().st_size}:{h.hexdigest()[:16]}"


def _measure_file(work_id: str, path: str) -> tuple[str, dict | None, str | None]:
    try:
        entry = measure(load_audio(Path(path)))
        entry["file_id"] = file_id(Path(path))
        entry["mtime_ns"] = Path(path).stat().st_mtime_ns
        return work_id, entry, None
    except Exception as exc:  # one bad file must not stop the batch
        return work_id, None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- cache
def load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in (doc.get("songs") or {}).items() if v.get("version") == ANALYSIS_VERSION}


def save_cache(path: Path, songs: dict) -> None:
    doc = {"version": ANALYSIS_VERSION, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "sr": SR, "hop": HOP, "songs": dict(sorted(songs.items()))}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def analyze_all(previews: dict[str, Path], cache_path: Path, *, force: bool = False, workers: int | None = None,
                log: Callable[[str], None] = print) -> tuple[dict, dict]:
    """Measure every preview not already cached with the same file id (size + SHA-1; a file
    whose size and modification time are unchanged is not re-hashed).
    Returns (songs, stats); the cache is saved every 50 songs, so an interrupted run resumes."""
    songs = {} if force else load_cache(cache_path)
    todo, touched = [], False
    for wid, p in sorted(previews.items()):
        e = songs.get(wid)
        st = p.stat()
        if e is not None and e.get("mtime_ns") == st.st_mtime_ns and e.get("file_id", "").split(":")[0] == str(st.st_size):
            continue                                   # unchanged since it was hashed
        if e is not None and e.get("file_id") == file_id(p):
            e["mtime_ns"] = st.st_mtime_ns             # same bytes, new timestamp: remember it
            touched = True
            continue
        todo.append((wid, str(p)))
    stats = dict(cached=len(previews) - len(todo), measured=0, failed={}, seconds=0.0)
    if not todo:
        if touched:
            save_cache(cache_path, songs)
        return songs, stats
    t0 = time.monotonic()
    workers = workers or max(1, min(4, os.cpu_count() or 2))
    log(f"measuring {len(todo)} previews with {workers} workers ...")
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_measure_file, wid, p) for wid, p in todo]
        for fut in as_completed(futures):
            wid, entry, err = fut.result()
            done += 1
            if entry is None:
                stats["failed"][wid] = err
            else:
                songs[wid] = entry
                stats["measured"] += 1
            if done % 50 == 0 or done == len(todo):
                log(f"  {done}/{len(todo)} ({time.monotonic() - t0:.0f}s)")
                save_cache(cache_path, songs)
    save_cache(cache_path, songs)
    stats["seconds"] = round(time.monotonic() - t0, 1)
    return songs, stats
