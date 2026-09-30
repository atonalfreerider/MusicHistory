"""End-to-end render of a synthetic tone: starts transposed and re-tempoed per the plan, glides
to native pitch, 44.1 kHz MP3 with the contract's fades. Skipped without ffmpeg/rubberband."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.paths import render  # noqa: E402
from musichistory.paths.ffmpeg import FfmpegNotFound, find_ffmpeg, has_filter  # noqa: E402
from musichistory.paths.plan import plan_step  # noqa: E402

soundfile = pytest.importorskip("soundfile")


@pytest.fixture(scope="module")
def ffmpeg():
    try:
        exe = find_ffmpeg()
    except FfmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not has_filter(exe, "rubberband"):
        pytest.skip("ffmpeg without rubberband")
    return exe


def tone(path: Path, seconds: float = 12.0, f0: float = 220.0, sr: int = 44100) -> None:
    t = np.arange(int(seconds * sr)) / sr
    y = sum(0.3 / k * np.sin(2 * np.pi * f0 * k * t) for k in (1, 2, 3))
    soundfile.write(str(path), np.stack([y, y], axis=1).astype(np.float32), sr)


def pitch(y: np.ndarray, sr: int) -> float:
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y)), n=1 << 17))
    freqs = np.fft.rfftfreq(1 << 17, 1 / sr)
    band = (freqs > 150) & (freqs < 400)
    return float(freqs[band][np.argmax(spec[band])])


def test_render_glides_from_start_key_to_native(tmp_path, ffmpeg):
    src = tmp_path / "src.wav"
    tone(src)
    plan = plan_step(0, 100.0, 9, 120.0, beats_per_bar=4)       # A heard as C: +3 semitones, 100 -> 120 BPM
    assert plan.start_semitones == 3
    dst = tmp_path / "out" / "01_Q1.mp3"
    seconds = render.render_step(ffmpeg, src, dst, plan)
    assert dst.exists()
    assert not list(dst.parent.glob("_*")), "temporary files left behind"
    info = soundfile.info(str(dst))
    assert info.samplerate == 44100
    # Output length: the glide consumes input_time(T) source seconds in T output seconds.
    T = plan.morph_seconds
    expected = T + (12.0 - plan.input_time(T))
    assert seconds == pytest.approx(expected, abs=0.25)
    y, sr = soundfile.read(str(dst))
    y = y.mean(axis=1)
    start = pitch(y[int(0.1 * sr):int(0.6 * sr)], sr)
    end = pitch(y[int((T + 1) * sr):int((T + 2.5) * sr)], sr)
    assert start == pytest.approx(220 * 2 ** (3 / 12), rel=0.03)
    assert end == pytest.approx(220, rel=0.02)
    # 30 ms fade-in and 1.5 s fade-out.
    assert np.abs(y[:40]).max() < 0.2 * np.abs(y[int(1 * sr):int(1.2 * sr)]).max()
    assert np.abs(y[-200:]).max() < 0.05 * np.abs(y[int(2 * sr):int(3 * sr)]).max()
    check = render.verify_start(dst, src, plan)
    assert check["ok"] and check["measured_semitones"] == 3


def test_first_step_plays_natively(tmp_path, ffmpeg):
    src = tmp_path / "src.wav"
    tone(src, seconds=6.0)
    plan = plan_step(None, None, 9, 120.0)
    dst = tmp_path / "01_Q1.mp3"
    seconds = render.render_step(ffmpeg, src, dst, plan)
    assert seconds == pytest.approx(6.0, abs=0.1)
    y, sr = soundfile.read(str(dst))
    assert pitch(y.mean(axis=1)[int(0.5 * sr):int(2 * sr)], sr) == pytest.approx(220, rel=0.02)


def test_filter_chain():
    glide = plan_step(0, 100.0, 2, 120.0)
    chain = render.filter_chain(glide, "cmds.txt")
    assert chain[0] == "asendcmd=f=cmds.txt"
    assert chain[1].startswith("rubberband=tempo=0.833333:pitch=0.890899:")
    assert "pitchq=quality" in chain[1] and "formant=preserved" in chain[1]
    assert chain[-2:] == [render.LOUDNORM, "aresample=44100"]
    native = plan_step(None, None, 2, 120.0)
    assert render.filter_chain(native, None) == [render.LOUDNORM, "aresample=44100"]
    lines = render.command_text(glide).splitlines()
    assert lines[-1].endswith("rubberband tempo 1.000000, rubberband pitch 1.000000;")
