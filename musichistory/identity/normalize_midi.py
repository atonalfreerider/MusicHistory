"""Write a song transposed to the target key and tempo (DESIGN.md §7, "apples to apples").

Every non-drum note is shifted by the normalization shift of the key region its note-on
falls in (so modulations disappear too), every tempo event is replaced by one
``TARGET_BPM`` tempo at tick 0, and old key signatures are replaced by the target key
(C major, or A minor / C minor for minor songs). Channel 10 (drums) is never transposed.

A note-off always gets the same shift as its note-on, even when a region boundary falls
inside a sustained note; otherwise the synth would be left with a hanging note.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from pathlib import Path

import mido

DRUM_CHANNEL0 = 9  # mido channels are 0-based


def _fold(p: int) -> int:
    while p > 127:
        p -= 12
    while p < 0:
        p += 12
    return p


def normalize_midi(src: str | Path, dst: str | Path, regions: Iterable, *, target_bpm: float,
                   key_signature: str | None = "C") -> Path:
    """``regions`` are objects with ``start`` (beat) and ``shift`` (semitones), or
    ``(start_beat, shift)`` pairs, sorted by start. Returns ``dst``."""
    regs = sorted(((float(r.start), int(r.shift)) if hasattr(r, "start") else (float(r[0]), int(r[1])))
                  for r in regions) or [(0.0, 0)]
    starts = [r[0] for r in regs]
    mid = mido.MidiFile(str(src), clip=True)
    ppq = mid.ticks_per_beat

    def shift_at(tick: int) -> int:
        i = bisect.bisect_right(starts, tick / ppq + 1e-6) - 1
        return regs[max(0, i)][1]

    out = mido.MidiFile(type=mid.type if mid.type in (0, 1) else 1, ticks_per_beat=ppq)
    for ti, track in enumerate(mid.tracks):
        events: list[tuple[int, mido.Message]] = []
        held: dict[tuple[int, int], list[int]] = {}
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.is_meta and msg.type in ("set_tempo", "key_signature"):
                continue
            if not msg.is_meta and msg.type in ("note_on", "note_off", "polytouch") and msg.channel != DRUM_CHANNEL0:
                k = (msg.channel, msg.note)
                if msg.type == "note_on" and msg.velocity > 0:
                    s = shift_at(tick)
                    held.setdefault(k, []).append(s)
                elif msg.type == "polytouch":
                    s = held[k][0] if held.get(k) else shift_at(tick)
                else:  # note-off (or note-on with velocity 0): FIFO, as NAudio pairs them
                    s = held[k].pop(0) if held.get(k) else shift_at(tick)
                msg = msg.copy(note=_fold(msg.note + s))
            events.append((tick, msg))
        if ti == 0:
            head = [mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(target_bpm), time=0)]
            if key_signature:
                head.append(mido.MetaMessage("key_signature", key=key_signature, time=0))
            events = [(0, m) for m in head] + events
        new = mido.MidiTrack()
        last = 0
        for t, m in events:
            new.append(m.copy(time=t - last))
            last = t
        out.tracks.append(new)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    out.save(str(tmp))
    tmp.replace(dst)
    return dst


def target_key_signature(mode: str, normalization: str) -> str:
    """mido key name of the target key: C major, A minor (relative) or C minor (parallel)."""
    if mode == "minor":
        return "Am" if normalization == "relative" else "Cm"
    return "C"
