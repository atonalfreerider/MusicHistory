"""Normalized MIDI: per-region pitch shifts, one TARGET_BPM tempo, drums untouched."""

from __future__ import annotations

import mido

from musichistory.identity.key import KeyRegion
from musichistory.identity.normalize_midi import normalize_midi, target_key_signature


def _abs(track):
    t = 0
    for msg in track:
        t += msg.time
        yield t, msg


def _track(events):
    tr = mido.MidiTrack()
    last = 0
    for t, msg in sorted(events, key=lambda e: e[0]):
        tr.append(msg.copy(time=t - last))
        last = t
    return tr


def _write(path, ppq=96):
    meta = [(0, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(90))),
            (0, mido.MetaMessage("key_signature", key="A")),
            (4 * ppq, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(140))),
            (12 * ppq, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(70)))]
    # A4 at beat 0 (region 1), C#5 held across the region boundary at beat 8, D5 at beat 12 (region 2).
    lead = []
    for on, off, p in ((0, 1, 69), (6, 10, 73), (12, 13, 74)):
        lead += [(on * ppq, mido.Message("note_on", channel=0, note=p, velocity=90)),
                 (off * ppq, mido.Message("note_off", channel=0, note=p, velocity=0))]
    drums = [(b * ppq, mido.Message("note_on", channel=9, note=36, velocity=100)) for b in range(16)]
    drums += [(b * ppq + 10, mido.Message("note_off", channel=9, note=36, velocity=0)) for b in range(16)]
    mid = mido.MidiFile(type=1, ticks_per_beat=ppq)
    for evs in (meta, lead, drums):
        mid.tracks.append(_track(evs))
    mid.save(str(path))
    return mid


def test_normalize_shifts_per_region_and_flattens_tempo(tmp_path):
    src = tmp_path / "in.mid"
    orig = _write(src)
    regions = [KeyRegion(0.0, 8.0, 9, "major", 3, 3), KeyRegion(8.0, 16.0, 2, "major", -2, -2)]
    dst = normalize_midi(src, tmp_path / "out.mid", regions, target_bpm=120.0, key_signature="C")
    out = mido.MidiFile(str(dst))
    assert out.ticks_per_beat == orig.ticks_per_beat and len(out.tracks) == len(orig.tracks)
    metas = [(t, m) for tr in out.tracks for t, m in _abs(tr) if m.is_meta]
    tempos = [(t, m.tempo) for t, m in metas if m.type == "set_tempo"]
    assert tempos == [(0, mido.bpm2tempo(120.0))]
    assert [m.key for _, m in metas if m.type == "key_signature"] == ["C"]
    lead = [(t, m.type, m.note) for t, m in _abs(out.tracks[1]) if m.type in ("note_on", "note_off")]
    ppq = out.ticks_per_beat
    assert lead == [(0, "note_on", 72), (ppq, "note_off", 72),          # A -> C (+3)
                    (6 * ppq, "note_on", 76), (10 * ppq, "note_off", 76),  # held across 8: keeps its +3
                    (12 * ppq, "note_on", 72), (13 * ppq, "note_off", 72)]  # D -> C (-2)
    drums_in = [(t, m.note) for t, m in _abs(orig.tracks[2]) if m.type == "note_on"]
    drums_out = [(t, m.note) for t, m in _abs(out.tracks[2]) if m.type == "note_on"]
    assert drums_in == drums_out


def test_target_key_signature():
    assert target_key_signature("major", "relative") == "C"
    assert target_key_signature("minor", "relative") == "Am"
    assert target_key_signature("minor", "parallel") == "Cm"


def test_pitches_fold_back_into_range(tmp_path):
    mid = mido.MidiFile(type=1, ticks_per_beat=96)
    tr = mido.MidiTrack([mido.Message("note_on", note=125, velocity=90, time=0),
                         mido.Message("note_off", note=125, velocity=0, time=96)])
    mid.tracks.append(tr)
    mid.save(str(tmp_path / "hi.mid"))
    out = mido.MidiFile(str(normalize_midi(tmp_path / "hi.mid", tmp_path / "o.mid", [(0.0, 6)], target_bpm=120.0)))
    notes = [m.note for m in out.tracks[0] if m.type in ("note_on", "note_off")]
    assert notes == [119, 119]
