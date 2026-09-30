"""ffmpeg discovery: $FFMPEG, then PATH, then the known install; never a hardcoded-only path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from musichistory.paths import ffmpeg as ff  # noqa: E402


def _exe(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"")
    return p


def test_env_override_wins(tmp_path):
    exe = _exe(tmp_path, "my-ffmpeg.exe")
    got = ff.find_ffmpeg(env={"FFMPEG": str(exe)}, which=lambda _: "/elsewhere/ffmpeg", known=())
    assert Path(got) == exe


def test_env_override_quoted(tmp_path):
    exe = _exe(tmp_path, "ffmpeg.exe")
    assert Path(ff.find_ffmpeg(env={"FFMPEG": f'"{exe}"'}, which=lambda _: None, known=())) == exe


def test_wrong_env_override_is_an_error(tmp_path):
    with pytest.raises(ff.FfmpegNotFound):
        ff.find_ffmpeg(env={"FFMPEG": str(tmp_path / "missing.exe")}, which=lambda _: "/usr/bin/ffmpeg", known=())


def test_path_before_known(tmp_path):
    known = _exe(tmp_path, "known.exe")
    got = ff.find_ffmpeg(env={}, which=lambda name: f"/on/path/{name}", known=(str(known),))
    assert got == "/on/path/ffmpeg"


def test_known_install_last(tmp_path):
    known = _exe(tmp_path, "known.exe")
    got = ff.find_ffmpeg(env={}, which=lambda _: None, known=(str(tmp_path / "nope.exe"), str(known)))
    assert got == str(known)


def test_not_found(tmp_path):
    with pytest.raises(ff.FfmpegNotFound):
        ff.find_ffmpeg(env={}, which=lambda _: None, known=(str(tmp_path / "nope.exe"),))


def test_known_path_is_the_documented_install():
    assert ff.KNOWN[0] == r"C:\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe"


def test_real_ffmpeg_has_the_render_filters():
    try:
        exe = ff.find_ffmpeg()
    except ff.FfmpegNotFound:
        pytest.skip("ffmpeg not installed")
    for name in ("rubberband", "asendcmd", "loudnorm", "afade", "aresample"):
        assert ff.has_filter(exe, name), name


@pytest.mark.parametrize("module", ["fetch_previews", "render_paths"])
def test_tools_do_not_hardcode_ffmpeg(module):
    text = (ROOT / "tools" / f"{module}.py").read_text(encoding="utf-8")
    assert "ffmpeg-8.1.1" not in text
