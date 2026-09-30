"""Render one path step with ffmpeg (docs: the paths.json contract in ``stage.py``).

Filter chain (source -> 44.1 kHz MP3):

    asendcmd (glide commands on the source timeline, ``StepPlan.commands``)
    -> rubberband (start tempo/pitch; pitchq=quality, formant=preserved, channels=together)
    -> loudnorm I=-16 LUFS, TP=-1.5 dB, LRA=11 -> aresample 44100
    then a second pass: 30 ms fade-in, 1.5 s fade-out, libmp3lame VBR q2.

A step that needs no glide (a path's first step, or same tonic and tempo) skips the
rubberband stage and plays natively. ``verify_start`` measures the transposition the render
actually starts with (chroma of its first 0.75 s against the source's, best circular shift).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np

from .plan import StepPlan, wrap

SAMPLE_RATE = 44100
FADE_IN, FADE_OUT = 0.03, 1.5
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
RUBBERBAND_OPTS = "pitchq=quality:formant=preserved:transients=mixed:channels=together"
RENDER_VERSION = 2


def filter_chain(plan: StepPlan, cmd_file: str | None) -> list[str]:
    filt = []
    if cmd_file and not plan.is_identity and plan.morph_seconds > 0:
        filt.append(f"asendcmd=f={cmd_file}")
        filt.append(f"rubberband=tempo={plan.start_ratio:.6f}:pitch={2 ** (plan.start_semitones / 12):.6f}:"
                    + RUBBERBAND_OPTS)
    filt += [LOUDNORM, f"aresample={SAMPLE_RATE}"]
    return filt


def command_text(plan: StepPlan) -> str:
    return "".join(f"{t:.4f} rubberband tempo {r:.6f}, rubberband pitch {p:.6f};\n" for t, r, p in plan.commands())


def _run(cmd: list[str], cwd: Path) -> None:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed ({r.returncode}): {r.stderr.strip()[-800:]}")


def render_step(ffmpeg: str, src: Path, dst: Path, plan: StepPlan) -> float:
    """Render ``src`` to ``dst`` per ``plan``; returns the output length in seconds."""
    import soundfile

    dst.parent.mkdir(parents=True, exist_ok=True)
    stem = dst.stem
    cmd_name = f"_{stem}.cmds.txt"
    tmp = dst.parent / f"_{stem}.tmp.wav"
    needs_glide = not plan.is_identity and plan.morph_seconds > 0
    try:
        if needs_glide:
            # Relative name + cwd: a Windows drive colon would need escaping inside a filter arg.
            (dst.parent / cmd_name).write_text(command_text(plan), encoding="ascii")
        chain = ",".join(filter_chain(plan, cmd_name if needs_glide else None))
        _run([ffmpeg, "-y", "-v", "error", "-i", str(Path(src).resolve()), "-af", chain, "-ar", str(SAMPLE_RATE),
              "-c:a", "pcm_s16le", tmp.name], dst.parent)
        length = float(soundfile.info(str(tmp)).duration)
        fades = f"afade=t=in:d={FADE_IN},afade=t=out:st={max(0.0, length - FADE_OUT):.3f}:d={FADE_OUT}"
        part = dst.parent / f"_{stem}.part.mp3"
        _run([ffmpeg, "-y", "-v", "error", "-i", tmp.name, "-af", fades, "-ar", str(SAMPLE_RATE),
              "-c:a", "libmp3lame", "-q:a", "2", part.name], dst.parent)
        os.replace(part, dst)
        return length
    finally:
        tmp.unlink(missing_ok=True)
        (dst.parent / cmd_name).unlink(missing_ok=True)
        (dst.parent / f"_{stem}.part.mp3").unlink(missing_ok=True)


def mp3_seconds(path: Path) -> float:
    import soundfile

    return float(soundfile.info(str(path)).duration)


def verify_start(render: Path, src: Path, plan: StepPlan, window: float = 0.75) -> dict:
    """Measured transposition at the render's start: the circular chroma shift that best maps
    the source's opening (``window`` × start ratio seconds) onto the render's first ``window``
    seconds. ``ok`` when it equals ``start_semitones`` (mod 12)."""
    import warnings

    import librosa

    sr = 22050
    y_r, _ = librosa.load(str(render), sr=sr, mono=True, duration=window)
    y_s, _ = librosa.load(str(src), sr=sr, mono=True, duration=window * max(plan.start_ratio, 0.1))
    if len(y_r) < sr // 4 or len(y_s) < sr // 4:
        return {"ok": None}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)      # short clips: librosa warns about n_fft
        c_r = librosa.feature.chroma_cqt(y=y_r, sr=sr).mean(axis=1)
        c_s = librosa.feature.chroma_cqt(y=y_s, sr=sr).mean(axis=1)
    scores = [float(np.corrcoef(np.roll(c_s, k), c_r)[0, 1]) for k in range(12)]
    best = int(np.argmax(scores))
    shift = wrap(best)
    return {"measured_semitones": shift, "corr": round(scores[best], 3),
            "ok": (shift - plan.start_semitones) % 12 == 0}
