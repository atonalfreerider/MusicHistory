"""tools/fetch_previews.py and tools/render_paths.py: importable without side effects, ffmpeg
found through musichistory.paths.ffmpeg, render_paths delegates to the 'paths' stage."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


def test_fetch_previews_imports_without_running(monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))
    mod = importlib.import_module("fetch_previews")
    assert calls == []
    assert mod.find_ffmpeg.__module__ == "musichistory.paths.ffmpeg"
    assert mod.norm("Stayin' Alive (Remastered)") == "stayin alive"
    assert mod.lead("Usher featuring will.i.am") == "Usher"


def test_fetch_previews_uses_ffmpeg_override(monkeypatch, tmp_path):
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("FFMPEG", str(exe))
    mod = importlib.import_module("fetch_previews")
    assert Path(mod.find_ffmpeg()) == exe


def test_render_paths_is_a_thin_wrapper(monkeypatch):
    mod = importlib.import_module("render_paths")
    assert mod.argv_for(["--pick-only"]) == ["paths", "--pick-only"]
    seen = []
    import musichistory.cli as cli
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    assert mod.main(["--force"]) == 0
    assert seen == [["paths", "--force"]]


def test_paths_stage_is_registered():
    from musichistory import cli
    assert cli.STAGES["paths"][0] == "musichistory.paths.stage"
    stage = importlib.import_module("musichistory.paths.stage")
    import argparse
    p = argparse.ArgumentParser()
    stage.add_arguments(p)
    a = p.parse_args(["--pick-only", "--force"])
    assert a.pick_only and a.force and not a.auto
