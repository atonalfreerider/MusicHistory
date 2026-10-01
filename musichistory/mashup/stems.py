"""Vocal / instrument stems with exactly Resonance-2's offline model.

Runs ``Resonance-2/Tools/SongLibrary/separate.py`` (Demucs 4.0.1 ``htdemucs``, shifts 1,
overlap 0.25, float WAV stems, no per-stem normalization) with Resonance-2's own venv, read
only: the model weights are read from Resonance-2's ``SongLibraryData/models/torch`` (the
script sets ``TORCH_HOME`` there) and every output goes to
``data/audio/stems/<work_id>/`` (``vocals``, ``drums``, ``bass``, ``other``, ``other-low``,
``other-high``, ``instruments`` = drums + bass + other, and ``separation.json``). The script
reads the preview MP3 itself (soundfile / libsndfile with MP3 support); it is not converted.

A folder whose ``separation.json`` names the preview's SHA-256 and whose stems exist is done
(resumable; ``force`` redoes it). ``quality`` is a cheap check of a finished separation:
stem levels, the share of the mix the vocal carries, the vocal energy left in
``instruments`` (its correlation with the vocal stem and the vocal-band energy in the
vocal's loud frames versus its quiet frames), and how exactly the stems add up to the mix.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .. import config

STEMS = ("vocals", "drums", "bass", "other", "instruments")
SEPARATOR_REL = Path("Tools") / "SongLibrary" / "separate.py"
PYTHON_REL = Path("Tools") / "SongLibrary" / ".venv" / "Scripts" / "python.exe"


def stems_root() -> Path:
    return config.DATA / "audio" / "stems"


def separator(resonance_root: Path | None = None) -> tuple[Path, Path]:
    """(python, separate.py) of the Resonance-2 checkout."""
    root = Path(resonance_root or config.RESONANCE_ROOT)
    py = root / PYTHON_REL
    if not py.is_file():
        py = root / "Tools" / "SongLibrary" / ".venv" / "bin" / "python"
    return py, root / SEPARATOR_REL


def sha256(path: Path) -> str:
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def is_done(src: Path, out: Path) -> bool:
    meta = out / "separation.json"
    if not meta.is_file() or not all((out / f"{s}.wav").is_file() for s in STEMS):
        return False
    try:
        doc = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return doc.get("sourceSha256") == sha256(src)


def separate(src: Path, out: Path, *, resonance_root: Path | None = None, device: str = "auto",
             timeout: float = 1800.0) -> float:
    """Run Resonance-2's separator on ``src`` into ``out``; returns wall seconds."""
    py, script = separator(resonance_root)
    if not py.is_file() or not script.is_file():
        raise FileNotFoundError(f"Resonance-2 separator not found: {py} / {script}")
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    r = subprocess.run([str(py), str(script), "--audio", str(Path(src).resolve()), "--output", str(out.resolve()),
                        "--device", device], cwd=str(script.parent), capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"separate.py failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-1200:]}")
    return time.monotonic() - t0


def separate_all(previews: dict[str, Path], *, force: bool = False, resonance_root: Path | None = None,
                 log: Callable[[str], None] = print) -> dict:
    """Separate every preview not done yet. Returns {done, separated, failed, seconds: {wid: s}}."""
    stats = {"done": 0, "separated": 0, "failed": {}, "seconds": {}}
    todo = []
    for wid, src in sorted(previews.items()):
        out = stems_root() / wid
        if not force and is_done(src, out):
            stats["done"] += 1
        else:
            todo.append((wid, src, out))
    for i, (wid, src, out) in enumerate(todo, 1):
        try:
            s = separate(src, out, resonance_root=resonance_root)
            stats["separated"] += 1
            stats["seconds"][wid] = round(s, 1)
            q = quality(src, out)
            log(f"  [{i}/{len(todo)}] stems {wid} in {s:.1f}s; vocal share {q['vocal_share']:.2f}, "
                f"residual vocal in instruments {q['instrument_vocal_corr']:+.2f} corr, "
                f"{q['vocal_band_leak_db']:+.1f} dB leak")
        except Exception as exc:  # one bad file must not stop the batch
            stats["failed"][wid] = f"{type(exc).__name__}: {exc}"
            log(f"  [{i}/{len(todo)}] stems {wid} FAILED: {stats['failed'][wid]}")
    return stats


def read_stem(out: Path, name: str) -> tuple[np.ndarray, int]:
    import soundfile

    y, sr = soundfile.read(str(out / f"{name}.wav"), dtype="float32", always_2d=True)
    return y, sr


def _db(x: float) -> float:
    return float(20 * np.log10(max(x, 1e-12)))


def quality(src: Path, out: Path) -> dict:
    """Cheap separation checks on a finished folder (all levels in dBFS RMS)."""
    import soundfile
    from scipy.signal import butter, sosfiltfilt

    mix, sr = soundfile.read(str(src), dtype="float32", always_2d=True)
    voc, _ = read_stem(out, "vocals")
    ins, _ = read_stem(out, "instruments")
    n = min(len(mix), len(voc), len(ins))
    mix, voc, ins = mix[:n].mean(axis=1), voc[:n].mean(axis=1), ins[:n].mean(axis=1)

    def rms(x):
        return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))

    resid = mix - voc - ins
    # Vocal-band (300 Hz - 3.4 kHz) energy of 'instruments' where the vocal is loud vs quiet:
    # a clean separation leaves no extra vocal-band energy when the singer sings.
    sos = butter(4, [300, 3400], btype="band", fs=sr, output="sos")
    ins_band = sosfiltfilt(sos, ins)
    hop = sr // 20
    frames = n // hop
    v_env = np.sqrt(np.mean(voc[: frames * hop].reshape(frames, hop) ** 2, axis=1))
    i_env = np.sqrt(np.mean(ins_band[: frames * hop].reshape(frames, hop) ** 2, axis=1))
    loud = v_env > np.percentile(v_env, 75)
    quiet = v_env < np.percentile(v_env, 25)
    leak = _db(float(np.mean(i_env[loud])) if loud.any() else 0) - _db(float(np.mean(i_env[quiet])) if quiet.any() else 0)
    corr = float(np.corrcoef(voc, ins)[0, 1]) if rms(voc) > 0 and rms(ins) > 0 else 0.0
    ev, ei = rms(voc) ** 2, rms(ins) ** 2
    return {"mix_db": round(_db(rms(mix)), 2), "vocals_db": round(_db(rms(voc)), 2),
            "instruments_db": round(_db(rms(ins)), 2), "vocal_share": round(ev / max(ev + ei, 1e-20), 3),
            "residual_db": round(_db(rms(resid)) - _db(rms(mix)), 2),
            "instrument_vocal_corr": round(corr, 3), "vocal_band_leak_db": round(leak, 2),
            "vocal_active_share": round(float(np.mean(v_env > 0.1 * v_env.max())) if v_env.max() > 0 else 0.0, 3)}
