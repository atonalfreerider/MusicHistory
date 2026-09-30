"""Synthetic songs for the identity tests (no text events of any kind)."""

from __future__ import annotations

from pathlib import Path

import mido

# I-vi-IV-V in C, one chord per bar; a stepwise tune on top.
PROGRESSION = [(60, (0, 4, 7)), (57, (0, 3, 7)), (53, (0, 4, 7)), (55, (0, 4, 7))]
TUNE = [72, 74, 76, 77, 79, 77, 76, 74]


def song_midi(path: Path, *, transpose: int = 0, bpm: float = 120.0, ppq: int = 480, bars: int = 32,
              tempo_changes: bool = False, type0: bool = False) -> Path:
    """Chords (ch 1), bass (ch 2), melody (ch 3), drums (ch 10), one track each."""
    def track(events: list[tuple[int, mido.Message]]) -> mido.MidiTrack:
        tr = mido.MidiTrack()
        last = 0
        for t, m in sorted(events, key=lambda e: (e[0], e[1].type == "note_on")):
            tr.append(m.copy(time=t - last))
            last = t
        return tr

    bar = 4 * ppq
    meta = [(0, mido.MetaMessage("time_signature", numerator=4, denominator=4)),
            (0, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm)))]
    if tempo_changes:
        meta += [(b * bar, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm * (0.8 + 0.05 * (b % 8)))))
                 for b in range(1, bars)]
    chords, bass, mel, drums = [], [], [], []
    for b in range(bars):
        root, iv = PROGRESSION[b % 4]
        t0 = b * bar
        for x in iv:
            chords += [(t0, mido.Message("note_on", channel=0, note=root + x + transpose, velocity=70)),
                       (t0 + bar - 10, mido.Message("note_off", channel=0, note=root + x + transpose, velocity=0))]
        for k in range(2):
            p = root - 24 + transpose
            bass += [(t0 + k * 2 * ppq, mido.Message("note_on", channel=1, note=p, velocity=90)),
                     (t0 + k * 2 * ppq + 2 * ppq - 10, mido.Message("note_off", channel=1, note=p, velocity=0))]
        for k in range(4):
            p = TUNE[(b * 4 + k) % len(TUNE)] + (b % 4 == 1) * -3 + transpose
            mel += [(t0 + k * ppq, mido.Message("note_on", channel=2, note=p, velocity=100)),
                    (t0 + k * ppq + ppq - 20, mido.Message("note_off", channel=2, note=p, velocity=0))]
        for k in range(8):
            n = 36 if k % 4 == 0 else (38 if k % 4 == 2 else 42)
            drums += [(t0 + k * ppq // 2, mido.Message("note_on", channel=9, note=n, velocity=100)),
                      (t0 + k * ppq // 2 + 30, mido.Message("note_off", channel=9, note=n, velocity=0))]
    mid = mido.MidiFile(type=1, ticks_per_beat=ppq)
    for evs in (meta, chords, bass, mel, drums):
        mid.tracks.append(track(evs))
    if type0:
        merged = mido.MidiFile(type=0, ticks_per_beat=ppq)
        merged.tracks.append(mido.merge_tracks(mid.tracks))
        mid = merged
    path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(path))
    return path


def slim(chords: list, notes: list, *, end_beat: float = 64.0, key_runs: list | None = None,
         measures: list | None = None, tempos: list | None = None, patterns: list | None = None,
         sections: list | None = None) -> dict:
    """A minimal slim analysis dict (DESIGN.md §7 schema)."""
    return {
        "version": 5, "slim_version": 1, "resonance_commit": "test", "midi_sha256": None, "style": "pop",
        "song_bars": int(end_beat // 4), "end_beat": end_beat, "duration_s": end_beat / 2,
        "tempos": tempos or [[0.0, 500000]], "measures": measures or [[0.0, 4, 4, int(end_beat // 4)]],
        "chords": chords, "sections": sections or [{"first_bar": 0, "bar_count": int(end_beat // 4), "family": 0,
                                                     "role": "Verse", "letter": "A", "start": 0.0, "end": end_beat,
                                                     "loops": 1, "cycle_beats": 16.0, "transpose": 0,
                                                     "variation": "fundamental", "group": -1}],
        "patterns": patterns or [], "groups": [], "form_grammar": "V",
        "key_changes": [], "key_runs": key_runs if key_runs is not None else [[0.0, end_beat, 0, False]],
        "notes": notes, "parts": [],
    }
