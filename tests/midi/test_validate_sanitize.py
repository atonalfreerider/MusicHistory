"""Validation and sanitization of broken and text-laden MIDI files (DESIGN.md §5)."""

from __future__ import annotations

import io
import json
import struct
import sys
from pathlib import Path

import mido
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import midigen  # noqa: E402
from musichistory.midi import InvalidMidi, process, sanitize, validate  # noqa: E402

FORBIDDEN_META = {"lyrics", "text", "marker", "cue_marker", "copyright", "instrument_name",
                  "sequencer_specific", "key_signature", "program_name", "device_name"}


ALLOWED_STRINGS = set(sanitize.ROLES) | set(sanitize.GM_FAMILIES) | {"drums", "name", "program", "channel", "none"}


def strings(obj) -> set[str]:
    if isinstance(obj, str):
        return {obj.split(":")[0]} if ":" in obj else {obj}
    if isinstance(obj, dict):
        return set().union(*(strings(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(strings(v) for v in obj)) if obj else set()
    return set()


def meta_types(data: bytes) -> set[str]:
    mid = mido.MidiFile(file=io.BytesIO(data))
    return {m.type for tr in mid.tracks for m in tr if m.is_meta}


def track_names(data: bytes) -> list[str]:
    mid = mido.MidiFile(file=io.BytesIO(data))
    return [next((m.name for m in tr if m.type == "track_name"), "") for tr in mid.tracks]


def test_clean_song_round_trip():
    p = process(midigen.song())
    assert p.ok, p.reason
    assert p.data[:4] == b"MThd"
    mid = mido.MidiFile(file=io.BytesIO(p.data))
    assert mid.type == 1 and mid.ticks_per_beat == midigen.PPQ
    assert track_names(p.data) == ["T00 other", "T01 vocal", "T02 bass", "T03 piano", "T04 drums"]
    f = p.features
    assert f["n_tracks"] == 5 and f["has_drums"]
    assert f["tempo"]["median_bpm"] == 120.0 and f["tempo"]["stable_fraction"] == 1.0
    assert f["time_signatures"] == [[0.0, 4, 4]]
    assert f["duration_s"] == pytest.approx(80.5, abs=0.6)  # 40 bars at 120 BPM + one padding beat
    roles = {t["index"]: (t["role"], t["role_src"], t["channel"]) for t in f["tracks"]}
    assert roles[1] == ("vocal", "name", 1) and roles[4] == ("drums", "name", 10)


@pytest.mark.parametrize("body,reason", [
    (b"", "empty"),
    (b"<!DOCTYPE html><html><body>500 Internal Server Error</body></html>", "html"),
    (b"  <html><head></head></html>", "html"),
    (b"<?xml version='1.0'?><error/>", "xml"),
    (b'{"error": "not found"}', "json"),
    (b"GIF89a....", "no_mthd"),
])
def test_rejects_non_midi_bodies(body, reason):
    with pytest.raises(InvalidMidi) as e:
        validate.parse(body)
    assert e.value.reason == reason
    assert process(body).reason == reason


def test_rmid_wrapper_unwrapped():
    p = process(midigen.rmid(midigen.song()))
    assert p.ok
    assert "rmid_unwrapped" in p.features["warnings"]
    assert p.data[:4] == b"MThd"


def test_format2_and_smpte_rejected():
    assert process(midigen.set_header(midigen.song(), fmt=2)).reason == "format2"
    smpte = (0xE7 << 8) | 40  # -25 fps, 40 ticks per frame
    assert process(midigen.set_header(midigen.song(), division=smpte)).reason == "smpte"


def test_truncated_file_is_repaired():
    data = midigen.song()
    cut = data[: int(len(data) * 0.7)]
    p = process(cut)
    assert p.ok, p.reason
    assert "truncated_file" in p.features["warnings"]
    # every track must end with end_of_track after the last note-off (NAudio requirement)
    mid = mido.MidiFile(file=io.BytesIO(p.data))
    for tr in mid.tracks:
        assert tr[-1].type == "end_of_track"


def test_unknown_chunk_skipped():
    data = midigen.insert_chunk_after_header(midigen.song(), b"XFIH", b"\x00" * 10)
    p = process(data)
    assert p.ok and "unknown_chunk" in p.features["warnings"]


def test_garbage_chunk_length_resyncs():
    data = bytearray(midigen.song())
    # corrupt the first MTrk length so it overruns into the next chunk
    i = data.find(b"MTrk")
    (n,) = struct.unpack(">I", data[i + 4:i + 8])
    data[i + 4:i + 8] = struct.pack(">I", n + 7)
    p = process(bytes(data))
    assert p.ok, p.reason


def test_no_notes_and_too_short_and_too_long():
    empty = mido.MidiFile(type=1)
    tr = mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=500000), mido.MetaMessage("end_of_track")])
    empty.tracks.append(tr)
    buf = io.BytesIO()
    empty.save(file=buf)
    assert process(buf.getvalue()).reason == "no_notes"
    assert process(midigen.song(bars=10)).reason == "too_short"  # 20 s at 120 BPM
    assert process(midigen.song(bars=820, bpm=400)).reason == "too_many_bars"


def test_karaoke_text_is_stripped_and_only_timing_kept():
    raw = midigen.karaoke_song()
    assert b"@KMIDI" in raw and b"Verse" in raw  # the fixture really carries text
    p = process(raw)
    assert p.ok, p.reason
    assert not (meta_types(p.data) & FORBIDDEN_META)
    for s in [b"@K", b"KARAOKE", b"Verse", b"Nobody", b"Placeholder", b"Song Title", b"Words", b"cue"]:
        assert s not in p.data
    # the features hold no free text at all: every string value is from a fixed vocabulary
    assert strings({k: v for k, v in p.features.items() if k != "warnings"}) <= ALLOWED_STRINGS
    assert all(w.split(":")[0].replace("_", "").isalnum() for w in p.features["warnings"])
    f = p.features
    n_onsets = 24 * 4  # one syllable per lead note
    assert f["lyric_events"] == n_onsets
    assert f["karaoke"] is True
    assert f["lyric_melody_track"] == 1 and f["lyric_f1"] == pytest.approx(1.0)
    assert f["marker_events"] == 1 and f["text_events"] == n_onsets + 2
    # both key signatures recorded before stripping: C major at 0, A minor at beat 40
    assert f["key_signatures"] == [[0.0, 0, 0], [40.0, 0, 1]]
    # the words-only track is gone; tracks are conductor + 4 instruments
    assert track_names(p.data) == ["T00 other", "T01 vocal", "T02 bass", "T03 piano", "T04 drums"]


def test_type0_is_split_per_channel():
    p = process(midigen.song(fmt=0))
    assert p.ok
    assert "type0_split" in p.features["warnings"]
    names = track_names(p.data)
    assert len(names) == 5 and names[0] == "T00 other"
    # names come from GM programs, not from the (title) track name of the type-0 file
    assert names[1:] == ["T01 backing", "T02 bass", "T03 piano", "T04 drums"]
    chans = [t["channel"] for t in p.features["tracks"]]
    assert chans == [1, 2, 3, 10]


def test_named_drums_move_to_channel_10():
    tracks = [
        {"name": "Melody", "channel": 0, "program": 73, "pitches": [72, 74, 76], "step": 1.0},
        {"name": "Drum Kit", "channel": 15, "program": 0, "pitches": [36, 38, 42], "step": 0.5},
    ]
    p = process(midigen.song(tracks=tracks))
    assert p.ok and "drums_to_ch10" in p.features["warnings"]
    drums = [t for t in p.features["tracks"] if t["role"] == "drums"][0]
    assert drums["channel"] == 10 and drums["is_drum"]


def test_hanging_notes_final_tick_and_padding():
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    cond = mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=500000), mido.MetaMessage("end_of_track")])
    tr = mido.MidiTrack()
    t = 0
    for i in range(80):  # 80 beats of quarter notes = 40 s
        tr.append(mido.Message("note_on", note=60 + i % 5, velocity=80, time=0 if i == 0 else 240))
        tr.append(mido.Message("note_off", note=60 + i % 5, velocity=0, time=240))
    tr.append(mido.Message("note_on", note=40, velocity=80, time=0))   # hanging: never released
    tr.append(mido.Message("note_on", note=90, velocity=80, time=480))  # note-on at the final tick
    tr.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks += [cond, tr]
    buf = io.BytesIO()
    mid.save(file=buf)
    raw = validate.parse(buf.getvalue())
    s = sanitize.sanitize(raw)
    w = s.facts.warnings
    assert "hanging_notes_closed:1" in w and "final_tick_note_on_dropped:1" in w
    out = mido.MidiFile(file=io.BytesIO(s.data))
    ends, last_off = [], 0
    for track in out.tracks:
        tick = 0
        for m in track:
            tick += m.time
            if m.type == "note_off" or (m.type == "note_on" and m.velocity == 0):
                last_off = max(last_off, tick)
            assert not (m.type == "note_on" and m.note == 90)
        ends.append(tick)
    assert set(ends) == {last_off + 480}  # every end_of_track exactly one beat after the last note-off


def test_running_status_and_bad_data_bytes():
    # hand-written track: running status note-ons and a corrupt data byte (0x80 -> clipped)
    trk = bytes([0x00, 0x90, 60, 100, 0x60, 62, 100, 0x60, 0x80, 60, 0x00, 0x00, 0x80, 62, 0x00])
    body = bytearray()
    for i in range(200):
        body += bytes([0x00 if i == 0 else 0x60, 0x90, 64, 90, 0x60, 0x80, 64, 0])
    trk += bytes(body) + b"\x00\xff\x2f\x00"
    data = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 96) + b"MTrk" + struct.pack(">I", len(trk)) + trk
    raw = validate.parse(data)
    assert raw.n_note_ons >= 200
    p = process(data)
    assert p.ok, p.reason


def test_sysex_kept_only_when_well_formed():
    good = mido.Message("sysex", data=[0x7E, 0x7F, 0x09, 0x01])  # GM system on
    p = process(midigen.song(extra_meta=[good]))
    assert p.ok
    mid = mido.MidiFile(file=io.BytesIO(p.data))
    assert any(m.type == "sysex" for tr in mid.tracks for m in tr)


def test_sanitized_output_is_stable():
    a = process(midigen.karaoke_song())
    b = process(a.data)  # sanitizing a sanitized file changes nothing musical
    assert b.ok
    assert b.data == a.data
