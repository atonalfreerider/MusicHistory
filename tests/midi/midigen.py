"""Tiny MIDI builders for the fetch/select tests (mido + raw bytes for broken files).

Placeholder lyric strings are made-up syllables ("la", "da", ...), never real lyrics.
"""

from __future__ import annotations

import io
import struct

import mido

PPQ = 480
FAKE_SYLLABLES = ["la", "da", "doo", "ba"]


def song(*, bars: int = 40, bpm: float = 120.0, tracks: list[dict] | None = None, fmt: int = 1,
         extra_meta: list[mido.MetaMessage] | None = None, ppq: int = PPQ) -> bytes:
    """A multi-track song: each track dict has name, channel (0-based), program, pitches, step (beats)."""
    tracks = tracks if tracks is not None else [
        {"name": "Lead Vocal", "channel": 0, "program": 52, "pitches": [60, 62, 64, 65, 67], "step": 1.0},
        {"name": "Bass", "channel": 1, "program": 33, "pitches": [36, 43], "step": 2.0},
        {"name": "Piano", "channel": 2, "program": 0, "pitches": [60, 64, 67], "step": 4.0, "chord": True},
        {"name": "Drums", "channel": 9, "program": 0, "pitches": [36, 38], "step": 1.0},
    ]
    mid = mido.MidiFile(type=1, ticks_per_beat=ppq)
    cond = mido.MidiTrack()
    cond.append(mido.MetaMessage("track_name", name="Song Title", time=0))
    cond.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    cond.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    for m in extra_meta or []:
        cond.append(m)
    cond.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(cond)
    total = bars * 4
    for spec in tracks:
        tr = mido.MidiTrack()
        if spec.get("name"):
            tr.append(mido.MetaMessage("track_name", name=spec["name"], time=0))
        ch = spec["channel"]
        tr.append(mido.Message("program_change", channel=ch, program=spec.get("program", 0), time=0))
        step = int(spec["step"] * ppq)
        dur = max(1, step - ppq // 8)
        t, i, last = 0, 0, 0
        while t + step <= total * ppq:
            notes = spec["pitches"] if spec.get("chord") else [spec["pitches"][i % len(spec["pitches"])]]
            for k, p in enumerate(notes):
                tr.append(mido.Message("note_on", channel=ch, note=p, velocity=90, time=(t - last) if k == 0 else 0))
            last = t
            for k, p in enumerate(notes):
                tr.append(mido.Message("note_off", channel=ch, note=p, velocity=0, time=dur if k == 0 else 0))
            last = t + dur
            t += step
            i += 1
        tr.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(tr)
    if fmt == 0:
        merged = mido.merge_tracks(mid.tracks)
        mid = mido.MidiFile(type=0, ticks_per_beat=ppq)
        mid.tracks.append(merged)
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def karaoke_song() -> bytes:
    """Lead track + a KAR words track whose syllables sit on the lead's note onsets."""
    mid = mido.MidiFile(file=io.BytesIO(song(bars=24)))
    lead = mid.tracks[1]
    onsets, t = [], 0
    for msg in lead:
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            onsets.append(t)
    words = mido.MidiTrack()
    words.append(mido.MetaMessage("track_name", name="Words", time=0))
    words.append(mido.MetaMessage("text", text="@KMIDI KARAOKE FILE", time=0))
    words.append(mido.MetaMessage("text", text="@TPlaceholder", time=0))
    last = 0
    for k, on in enumerate(onsets):
        syl = FAKE_SYLLABLES[k % len(FAKE_SYLLABLES)]
        words.append(mido.MetaMessage("text", text=syl, time=on - last))
        words.append(mido.MetaMessage("lyrics", text=syl, time=0))
        if on == 480 * 40:  # a conflicting key signature in another track, at beat 40
            words.append(mido.MetaMessage("key_signature", key="Am", time=0))
        last = on
    words.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(words)
    cond = mid.tracks[0]
    cond.insert(1, mido.MetaMessage("copyright", text="(c) Nobody", time=0))
    cond.insert(1, mido.MetaMessage("marker", text="Verse", time=0))
    cond.insert(1, mido.MetaMessage("key_signature", key="C", time=0))
    cond.insert(1, mido.MetaMessage("cue_marker", text="cue", time=0))
    cond.insert(1, mido.MetaMessage("sequencer_specific", data=[1, 2, 3], time=0))
    lead.insert(1, mido.MetaMessage("instrument_name", name="Voice Placeholder", time=0))
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def set_header(data: bytes, *, fmt: int | None = None, division: int | None = None) -> bytes:
    hlen, f, n, d = struct.unpack(">IHHH", data[4:14])
    return data[:4] + struct.pack(">IHHH", hlen, f if fmt is None else fmt, n,
                                  d if division is None else division) + data[14:]


def rmid(data: bytes) -> bytes:
    body = b"RMID" + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def insert_chunk_after_header(data: bytes, cid: bytes, payload: bytes) -> bytes:
    return data[:14] + cid + struct.pack(">I", len(payload)) + payload + data[14:]
