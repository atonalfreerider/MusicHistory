"""``features_json`` for a sanitized candidate (schema in DESIGN.md §5).

Computed by re-reading the sanitized bytes with mido, which doubles as a check that the
canonical file parses with a standard reader. Pre-strip facts (key signatures, lyric tick
positions, name-derived roles) come from ``sanitize.Facts``. Lyric timing is used only to
find the track the lyrics follow (F1 of lyric ticks against note onsets, mir-methods §3.1).
"""

from __future__ import annotations

import bisect
import io
from collections import Counter

import mido

from .sanitize import DRUM_CHANNEL, Facts, gm_family

LYRIC_F1_MIN = 0.6
DEFAULT_TEMPO = 500_000


def _r(x: float, nd: int = 3) -> float:
    return round(float(x), nd)


def _tempo_segments(tempos: list[tuple[int, int]], end_tick: int) -> list[tuple[int, int, int]]:
    """[(start_tick, end_tick, uspq)] covering 0..end_tick."""
    tempos = sorted(tempos)
    if not tempos or tempos[0][0] > 0:
        tempos = [(0, DEFAULT_TEMPO)] + tempos
    segs = []
    for i, (t, v) in enumerate(tempos):
        nxt = tempos[i + 1][0] if i + 1 < len(tempos) else end_tick
        if nxt > t:
            segs.append((t, min(nxt, end_tick), v))
    return segs or [(0, max(end_tick, 1), tempos[-1][1])]


def _tempo_stats(segs: list[tuple[int, int, int]], ppq: int, n_events: int) -> dict:
    rows = [(6e7 / v, (b - a) / ppq) for a, b, v in segs if b > a]
    sane = [(bpm, w) for bpm, w in rows if 20 <= bpm <= 400] or rows
    total = sum(w for _, w in sane) or 1.0
    acc, median = 0.0, sane[0][0] if sane else 120.0
    for bpm, w in sorted(sane):
        acc += w
        if acc >= total / 2:
            median = bpm
            break
    stable = sum(w for bpm, w in sane if abs(bpm - median) <= 0.05 * median) / total
    return {
        "n_events": n_events,
        "median_bpm": _r(median, 2),
        "min_bpm": _r(min(b for b, _ in sane), 2) if sane else 120.0,
        "max_bpm": _r(max(b for b, _ in sane), 2) if sane else 120.0,
        "stable_fraction": _r(stable),
    }


def _seconds(segs: list[tuple[int, int, int]], ppq: int) -> float:
    return sum((b - a) / ppq * v / 1e6 for a, b, v in segs)


def _bars(timesigs: list[tuple[int, int, int]], end_tick: int, ppq: int) -> float:
    ts = sorted(timesigs) or [(0, 4, 4)]
    if ts[0][0] > 0:
        ts = [(0, 4, 4)] + ts
    bars = 0.0
    for i, (t, num, den) in enumerate(ts):
        nxt = ts[i + 1][0] if i + 1 < len(ts) else end_tick
        if nxt > t:
            bars += (nxt - t) / ppq / (num * 4.0 / den)
    return bars


def _union_length(intervals: list[tuple[int, int]]) -> int:
    total, cur_a, cur_b = 0, None, None
    for a, b in sorted(intervals):
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


def _polyphony(notes: list[tuple[int, int, int, int, int]]) -> float:
    """Fraction of notes that start while another note of the track sounds or starts."""
    if not notes:
        return 0.0
    ons = Counter(n[0] for n in notes)
    poly, max_off = 0, -1
    for on, off, *_ in sorted(notes):
        if ons[on] > 1 or max_off > on:
            poly += 1
        max_off = max(max_off, off)
    return poly / len(notes)


def _skyline_interval(notes: list[tuple[int, int, int, int, int]]) -> float:
    top: dict[int, int] = {}
    for on, _, pitch, *_ in notes:
        if pitch > top.get(on, -1):
            top[on] = pitch
    seq = [top[k] for k in sorted(top)]
    if len(seq) < 2:
        return 0.0
    return sum(abs(b - a) for a, b in zip(seq, seq[1:])) / (len(seq) - 1)


def lyric_f1(lyric_ticks: list[int], onsets: list[int], tol: int) -> float:
    """F1 between lyric-event ticks and a track's note onsets (tolerance in ticks)."""
    if not lyric_ticks or not onsets:
        return 0.0
    onsets = sorted(set(onsets))
    lyr = sorted(set(lyric_ticks))

    def hits(src: list[int], ref: list[int]) -> int:
        n = 0
        for t in src:
            i = bisect.bisect_left(ref, t - tol)
            if i < len(ref) and ref[i] <= t + tol:
                n += 1
        return n

    p = hits(lyr, onsets) / len(lyr)
    r = hits(onsets, lyr) / len(onsets)
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def compute(data: bytes, facts: Facts) -> dict:
    mid = mido.MidiFile(file=io.BytesIO(data), clip=True)
    ppq = mid.ticks_per_beat
    tempos: list[tuple[int, int]] = []
    timesigs: list[tuple[int, int, int]] = []
    end_tick = 0
    per_track: list[dict] = []
    first_prog: dict[int, int] = {}
    all_on: list[int] = []
    all_off: list[int] = []
    for ti, track in enumerate(mid.tracks):
        tick = 0
        open_notes: dict[tuple[int, int], list[tuple[int, int]]] = {}
        notes: list[tuple[int, int, int, int, int]] = []  # on, off, pitch, vel, channel0
        progs: dict[int, int] = {}
        for msg in track:
            tick += msg.time
            t = msg.type
            if t == "note_on" and msg.velocity > 0:
                open_notes.setdefault((msg.channel, msg.note), []).append((tick, msg.velocity))
            elif t in ("note_off", "note_on"):
                q = open_notes.get((msg.channel, msg.note))
                if q:  # FIFO pairing, as in sanitize and NAudio
                    st = q.pop(0)
                    notes.append((st[0], tick, msg.note, st[1], msg.channel))
            elif t == "program_change":
                progs.setdefault(msg.channel, msg.program)
                first_prog.setdefault(msg.channel, msg.program)
            elif t == "set_tempo":
                tempos.append((tick, msg.tempo))
            elif t == "time_signature":
                timesigs.append((tick, msg.numerator, msg.denominator))
        end_tick = max(end_tick, tick)
        per_track.append({"index": ti, "notes": notes, "progs": progs})
        all_on += [n[0] for n in notes]
        all_off += [n[1] for n in notes]

    segs = _tempo_segments(tempos, end_tick)
    span_a = min(all_on, default=0)
    span_b = max(all_off, default=0)
    span = max(1, span_b - span_a)
    tol = max(1, ppq // 32)

    tracks_out = []
    n_drum = n_pitched = 0
    best_lyric = (0.0, None)
    for tr in per_track:
        notes = tr["notes"]
        if not notes:
            continue
        chans = Counter(n[4] for n in notes)
        ch = chans.most_common(1)[0][0]
        is_drum = ch == DRUM_CHANNEL
        program = tr["progs"].get(ch, first_prog.get(ch, 0))
        role, role_src = facts.track_roles[tr["index"]] if tr["index"] < len(facts.track_roles) else ("other", "none")
        pitches = [n[2] for n in notes]
        if is_drum:
            n_drum += len(notes)
        else:
            n_pitched += len(notes)
            if facts.lyric_ticks:
                f1 = lyric_f1(facts.lyric_ticks, [n[0] for n in notes], tol)
                if f1 > best_lyric[0]:
                    best_lyric = (f1, tr["index"])
        tracks_out.append({
            "index": tr["index"], "channel": ch + 1, "program": program,
            "family": "drums" if is_drum else gm_family(program), "is_drum": is_drum,
            "role": role, "role_src": role_src, "n_notes": len(notes),
            "mean_pitch": _r(sum(pitches) / len(pitches), 2), "min_pitch": min(pitches), "max_pitch": max(pitches),
            "polyphony": _r(_polyphony(notes)),
            "occupation": _r(_union_length([(a, b) for a, b, *_ in notes]) / span),
            "mean_abs_interval": _r(_skyline_interval(notes), 2),
            "mean_velocity": _r(sum(n[3] for n in notes) / len(notes), 1),
            "first_beat": _r(min(n[0] for n in notes) / ppq, 2),
            "last_beat": _r(max(n[1] for n in notes) / ppq, 2),
        })

    ts_rows: list[list[float]] = []
    for t, num, den in sorted(timesigs):
        row = [_r(t / ppq, 4), num, den]
        if not ts_rows or ts_rows[-1][1:] != row[1:]:
            ts_rows.append(row)
    lyric_track = best_lyric[1] if best_lyric[0] >= LYRIC_F1_MIN else None
    return {
        "format": mid.type, "ppq": ppq, "n_tracks": len(mid.tracks),
        "duration_s": _r(_seconds(segs, ppq), 2), "end_beat": _r(end_tick / ppq, 3),
        "n_bars": _r(_bars(timesigs, end_tick, ppq), 1),
        "n_notes": n_drum + n_pitched, "n_pitched_notes": n_pitched, "n_drum_notes": n_drum,
        "has_drums": n_drum > 0,
        "tempo": _tempo_stats(segs, ppq, len(tempos)),
        "time_signatures": ts_rows,
        "key_signatures": facts.key_signatures,
        "lyric_events": facts.lyric_events, "text_events": facts.text_events,
        "marker_events": facts.marker_events,
        "lyric_melody_track": lyric_track,
        "lyric_f1": _r(best_lyric[0]) if facts.lyric_ticks else None,
        "karaoke": facts.karaoke,
        "tracks": tracks_out,
        "warnings": facts.warnings,
    }
