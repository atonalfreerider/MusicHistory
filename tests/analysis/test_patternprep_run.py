"""PatternPrep end to end: build, run, slim, and failure handling (runs the real exe).

The build goes to the shared ``data/tools/PatternPrep`` (a build cache, rebuilt only when
the Resonance-2 sources change); every analysis runs in a pytest tmp folder.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "identity"))
from id_helpers import song_midi  # noqa: E402

from musichistory.analysis import patternprep as pp  # noqa: E402
from musichistory.identity import extract  # noqa: E402


@pytest.fixture(scope="module")
def exe():
    return pp.ensure_built()


def test_build_and_commit(exe):
    assert exe.exists()
    assert re.fullmatch(r"[0-9a-f]{40}(\+dirty\.[0-9a-f]{10})?|nogit\.[0-9a-f]{12}", pp.resonance_commit())
    stamp = json.loads((pp.tool_dir() / "musichistory-build.json").read_text(encoding="utf-8"))
    assert stamp["sources_sha256"] == pp.sources_sha256() and stamp["commit"] == pp.resonance_commit()
    assert pp.ensure_built() == exe  # no rebuild when nothing changed


def test_analyze_synthetic_song(exe, tmp_path):
    midi = song_midi(tmp_path / "in" / "song.mid")
    work = tmp_path / "work"
    work.mkdir()
    (work / "lyrics.txt").write_text("stale file that must never be read", encoding="utf-8")
    slim = pp.analyze(midi, work, lead_track=3)
    assert slim["sections"] and slim["patterns"] and slim["style"] == "pop"
    assert slim["tempos"] == [[0.0, 500000]] and slim["measures"][0][1:3] == [4, 4]
    roots = {c[2] for c in slim["chords"] if c[2] >= 0}
    assert roots <= {0, 9, 5, 7} and {0, 9, 5, 7} <= roots   # C Am F G, pitch classes C = 0
    assert "Lyrics" not in slim and "lyric" not in json.dumps(slim).lower()
    assert sorted(p.name for p in work.iterdir()) == ["analysis.json", "score.mid", "song.json"]
    assert json.loads((work / "song.json").read_text()) == {"Style": "pop", "LeadVocalTrack": 3}
    assert json.loads((work / "analysis.json").read_text(encoding="utf-8")) == slim
    ident = extract.extract(slim)
    assert ident.key_name == "C major" and ident.native_bpm == 120.0
    assert any(lp.cycle_id == "0.28.15.21" for lp in ident.loops)  # I-vi-IV-V cycle


def test_invariance_transposed_tempo_ppq_type0(exe, tmp_path):
    base = extract.extract(pp.analyze(song_midi(tmp_path / "a.mid"), tmp_path / "wa"))
    variants = {
        "transposed": song_midi(tmp_path / "b.mid", transpose=5),
        "tempo": song_midi(tmp_path / "c.mid", bpm=97.0, tempo_changes=True),
        "ppq": song_midi(tmp_path / "d.mid", ppq=96),
        "type0": song_midi(tmp_path / "e.mid", type0=True),
    }
    for name, path in variants.items():
        v = extract.extract(pp.analyze(path, tmp_path / f"w-{name}"))
        assert v.tokens() == base.tokens(), name
        assert [lp.cycle_id for lp in v.loops] == [lp.cycle_id for lp in base.loops], name
        assert v.melody.pitches == base.melody.pitches and v.melody.onsets == base.melody.onsets, name
    assert extract.extract(pp.analyze(variants["transposed"], tmp_path / "w2")).key_name == "F major"


def test_errors_are_analysis_errors(exe, tmp_path):
    html = tmp_path / "html.mid"
    html.write_bytes(b"<html>not a midi</html>")
    with pytest.raises(pp.AnalysisError, match="MThd"):
        pp.analyze(html, tmp_path / "w-html")
    # A valid header with a truncated track crashes PatternPrep: a clean AnalysisError.
    bad = tmp_path / "bad.mid"
    bad.write_bytes(b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0MTrk\x00\x00\x00\x40\x00\x90\x3c")
    with pytest.raises(pp.AnalysisError, match="exit"):
        pp.analyze(bad, tmp_path / "w-bad")
    assert not (tmp_path / "w-bad" / "score.mid.patterns.json").exists()
    import mido
    empty = mido.MidiFile(type=1, ticks_per_beat=480)
    empty.tracks.append(mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=500000, time=0)]))
    empty.save(str(tmp_path / "empty.mid"))
    with pytest.raises(pp.AnalysisError, match="0 sections|no pitched notes"):
        pp.analyze(tmp_path / "empty.mid", tmp_path / "w-empty")
    assert not (tmp_path / "w-empty" / "analysis.json").exists()
    with pytest.raises(pp.AnalysisError, match="timed out"):
        pp.analyze(song_midi(tmp_path / "t.mid"), tmp_path / "w-timeout", timeout=0.05)
    with pytest.raises(pp.AnalysisError, match="not found"):
        pp.analyze(tmp_path / "missing.mid", tmp_path / "w-missing")
