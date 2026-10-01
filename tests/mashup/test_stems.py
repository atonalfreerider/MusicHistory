"""Stem bookkeeping: done/resume detection, the Resonance-2 separator's location, the batch
loop (with the separator stubbed) and the cheap quality measures."""

from __future__ import annotations

import json

import numpy as np
import pytest

from musichistory.mashup import stems

soundfile = pytest.importorskip("soundfile")
SR = 44100


def write_stems(out, voc, ins, src):
    out.mkdir(parents=True, exist_ok=True)
    for name in stems.STEMS:
        sig = {"vocals": voc, "instruments": ins}.get(name, ins / 3)
        soundfile.write(str(out / f"{name}.wav"), np.stack([sig, sig], axis=1), SR, subtype="FLOAT")
    (out / "separation.json").write_text(json.dumps({"sourceSha256": stems.sha256(src)}), encoding="utf-8")


@pytest.fixture()
def song(tmp_path):
    t = np.arange(SR * 3) / SR
    voc = (0.2 * np.sin(2 * np.pi * 440 * t) * (t % 1 < 0.5)).astype(np.float32)
    ins = (0.2 * np.sin(2 * np.pi * 110 * t)).astype(np.float32)
    src = tmp_path / "preview.wav"
    soundfile.write(str(src), np.stack([voc + ins] * 2, axis=1), SR)
    return src, voc, ins


def test_is_done_needs_all_stems_and_matching_source(tmp_path, song):
    src, voc, ins = song
    out = tmp_path / "stems" / "Q1"
    assert not stems.is_done(src, out)
    write_stems(out, voc, ins, src)
    assert stems.is_done(src, out)
    (out / "bass.wav").unlink()
    assert not stems.is_done(src, out)
    write_stems(out, voc, ins, src)
    (out / "separation.json").write_text(json.dumps({"sourceSha256": "0" * 64}), encoding="utf-8")
    assert not stems.is_done(src, out)                       # the preview changed: redo


def test_quality_measures(tmp_path, song):
    src, voc, ins = song
    out = tmp_path / "stems" / "Q1"
    write_stems(out, voc, ins, src)
    q = stems.quality(src, out)
    assert q["residual_db"] < -60                             # stems add up to the mix
    assert 0.1 < q["vocal_share"] < 0.5
    assert abs(q["instrument_vocal_corr"]) < 0.05
    assert q["vocal_active_share"] == pytest.approx(0.5, abs=0.05)


def test_separator_paths(tmp_path):
    py, script = stems.separator(tmp_path)
    assert script == tmp_path / "Tools" / "SongLibrary" / "separate.py"
    with pytest.raises(FileNotFoundError):
        stems.separate(tmp_path / "x.mp3", tmp_path / "out", resonance_root=tmp_path)


def test_separate_all_resumes(tmp_path, song, monkeypatch):
    src, voc, ins = song
    monkeypatch.setattr(stems, "stems_root", lambda: tmp_path / "stems")
    calls = []

    def fake(s, out, **kw):
        calls.append(out.name)
        write_stems(out, voc, ins, s)
        return 1.5

    monkeypatch.setattr(stems, "separate", fake)
    st = stems.separate_all({"Q1": src, "Q2": src}, log=lambda m: None)
    assert st["separated"] == 2 and st["done"] == 0 and st["seconds"] == {"Q1": 1.5, "Q2": 1.5}
    st = stems.separate_all({"Q1": src, "Q2": src}, log=lambda m: None)
    assert st["separated"] == 0 and st["done"] == 2 and calls == ["Q1", "Q2"]


def test_real_separator_is_the_resonance_model():
    """The separator Resonance-2 ships is Demucs htdemucs with shifts 1, overlap .25 (read only)."""
    _, script = stems.separator()
    if not script.is_file():
        pytest.skip("Resonance-2 not checked out")
    text = script.read_text(encoding="utf-8")
    assert "MODEL = 'htdemucs'" in text and "shifts=1" in text and "overlap=.25" in text
