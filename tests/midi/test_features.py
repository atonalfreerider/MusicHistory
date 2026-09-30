"""features_json schema and values (DESIGN.md §5)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import mido
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import midigen  # noqa: E402
from musichistory.midi import features, process  # noqa: E402

SCHEMA_KEYS = {"format", "ppq", "n_tracks", "duration_s", "end_beat", "n_notes", "n_pitched_notes",
               "n_drum_notes", "has_drums", "tempo", "time_signatures", "key_signatures", "lyric_events",
               "text_events", "marker_events", "lyric_melody_track", "lyric_f1", "karaoke", "tracks", "warnings"}
TRACK_KEYS = {"index", "channel", "program", "family", "is_drum", "role", "n_notes", "mean_pitch", "min_pitch",
              "max_pitch", "polyphony", "occupation", "mean_abs_interval", "mean_velocity"}


def test_schema_keys_present():
    f = process(midigen.song()).features
    assert SCHEMA_KEYS <= set(f)
    assert set(f["tempo"]) == {"n_events", "median_bpm", "min_bpm", "max_bpm", "stable_fraction"}
    for t in f["tracks"]:
        assert TRACK_KEYS <= set(t)
        assert 1 <= t["channel"] <= 16
        assert t["role"] in ("vocal", "melody", "lead", "bass", "drums", "backing", "piano", "guitar", "strings", "other")


def test_counts_and_track_stats():
    f = process(midigen.song(bars=40)).features
    by_role = {t["role"]: t for t in f["tracks"]}
    # 40 bars: vocal quarter notes (160), bass half notes (80), piano whole-note triads (120), drums (160)
    assert by_role["vocal"]["n_notes"] == 160
    assert by_role["bass"]["n_notes"] == 80
    assert by_role["piano"]["n_notes"] == 120
    assert by_role["drums"]["n_notes"] == 160 and by_role["drums"]["is_drum"]
    assert f["n_notes"] == 520 and f["n_drum_notes"] == 160 and f["n_pitched_notes"] == 360
    assert by_role["vocal"]["polyphony"] == 0.0
    assert by_role["piano"]["polyphony"] == 1.0          # every onset is a chord
    assert by_role["vocal"]["occupation"] == pytest.approx(0.875, abs=0.01)  # 7/8 of each beat sounds
    assert by_role["vocal"]["mean_abs_interval"] == pytest.approx((2 + 2 + 1 + 2 + 7) / 5, abs=0.05)
    assert by_role["vocal"]["family"] == "ensemble" and by_role["bass"]["family"] == "bass"
    assert f["n_bars"] == pytest.approx(160.875 / 4, abs=0.05)  # last note-off + one padding beat


def test_tempo_map_stats():
    mid = mido.MidiFile(file=io.BytesIO(midigen.song(bars=40, bpm=100)))
    cond = mid.tracks[0]
    # 100 BPM for 120 beats, then 150 BPM for the last 40 beats
    cond.insert(-1, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(150), time=480 * 120))
    buf = io.BytesIO()
    mid.save(file=buf)
    f = process(buf.getvalue()).features
    t = f["tempo"]
    assert t["n_events"] == 2
    assert t["median_bpm"] == 100.0 and t["min_bpm"] == 100.0 and t["max_bpm"] == 150.0
    assert t["stable_fraction"] == pytest.approx(120 / 160.875, abs=0.01)
    assert f["duration_s"] == pytest.approx(120 * 0.6 + 40.875 * 0.4, abs=0.01)


def test_lyric_f1():
    tol = 15
    onsets = [0, 480, 960, 1440]
    assert features.lyric_f1([0, 480, 960, 1440], onsets, tol) == 1.0
    assert features.lyric_f1([5, 470, 2000], onsets, tol) == pytest.approx(2 * (2 / 3) * 0.5 / ((2 / 3) + 0.5))
    assert features.lyric_f1([], onsets, tol) == 0.0


def test_no_lyrics_means_null_lyric_fields():
    f = process(midigen.song()).features
    assert f["lyric_events"] == 0 and f["lyric_f1"] is None and f["lyric_melody_track"] is None
    assert f["karaoke"] is False
