"""Lead line and bass line on the beat grid (mir-methods.md §3.1-3.2).

Lead selection, first that succeeds (DESIGN.md §7):

1. **name**: a track the sanitizer named ``vocal``/``melody`` from its original name
   (``features.tracks[].role``, ``role_src == 'name'``); then ``lead``-named tracks after 2;
2. **lyric_timing**: the track whose onsets match the karaoke lyric-event ticks
   (``features.lyric_melody_track``, F1 >= 0.6; lyric text never reaches us);
3. **classifier**: per-lane heuristic of Rizo et al. 2006 (mir-methods §3.1)
   ``1.5 occ - 3 poly + [55 <= mean pitch <= 80] - 0.4 max(0, mean|int| - 4)
   + 0.5 [melodic GM program] + 0.3 z(velocity)``, gated at >= 64 notes, occupation
   >= 0.25, polyphony <= 0.3 and normalized mean pitch >= 50. Polyphony here is the share
   of onsets that start a chord of 3+ notes (a tune doubled in thirds is still a tune);
4. **skyline** over every pitched note at (normalized) pitch >= 55.

Register tests use *normalized* pitches (native + region shift), so the choice does not
depend on the key the file was written in: a transposed copy picks the same lane.

The chosen notes become one line: skyline (highest note per onset, cut at the next onset),
grace notes removed (< 1/8 beat before a note within 2 semitones), onsets quantized to
1/12 beat (16ths and triplets) of their bar's grid, rests absorbed (each note lasts until
the next onset).
The bass line is the lowest-note skyline of the bass lane, cleaned the same way.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from .meter import Meter

DRUMS = 10
GRID = 12
GRACE_BEATS = 0.125
MELODIC_PROGRAMS = set(range(52, 55)) | set(range(56, 69)) | set(range(71, 74)) | set(range(80, 88))
NAME_MIN_NOTES = 32
CLASSIFIER_MIN_NOTES = 64
CLASSIFIER_MAX_POLY = 0.3     # share of onsets starting a >= 3-note chord
CLASSIFIER_MIN_PITCH = 50.0   # normalized mean pitch; a lead line below D3 is a bass part

Note = list  # [beat, length, pitch, track, channel, velocity]
Lane = tuple[int, int]  # (track, channel)


@dataclass
class Line:
    onsets: list[float]
    durs: list[float]
    pitches: list[int]        # normalized MIDI pitches (native + region shift)
    met: list[int]
    track: int | None
    channel: int | None
    method: str
    confidence: float

    def __len__(self) -> int:
        return len(self.pitches)


@dataclass
class LaneStats:
    lane: Lane
    n: int
    occupation: float
    polyphony: float
    mean_pitch: float         # normalized
    mean_abs_interval: float
    mean_velocity: float


# --------------------------------------------------------------------------- lines
def skyline(notes: list[Note], highest: bool = True) -> list[tuple[float, float, int]]:
    """(onset, duration, pitch): the highest (or lowest) note at each onset, cut at the next."""
    best: dict[float, Note] = {}
    for n in notes:
        cur = best.get(n[0])
        if cur is None or (n[2] > cur[2] if highest else n[2] < cur[2]):
            best[n[0]] = n
    onsets = sorted(best)
    out = []
    for i, t in enumerate(onsets):
        n = best[t]
        end = t + n[1]
        if i + 1 < len(onsets):
            end = min(end, onsets[i + 1])
        out.append((t, max(end - t, 0.0), int(n[2])))
    return out


def quantize(t: float, meter: Meter | None = None) -> float:
    """Onset on the 1/12-beat grid of its own bar (unrounded). Bar lines need not sit on
    multiples of 1/12 (a first time signature at tick 100 of PPQ 384 puts every bar line at
    x.2604); snapping to an absolute grid would move every note off its bar line and lose
    the downbeats. Without a meter (or with bars on the absolute grid) this is round(t * 12) / 12."""
    origin = meter.bar_start(t) if meter is not None else 0.0
    return origin + math.floor((t - origin) * GRID + 0.5) / GRID


def cleanup(line: list[tuple[float, float, int]], *, highest: bool = True,
            meter: Meter | None = None) -> list[tuple[float, float, int]]:
    """Grace notes out, onsets on the 1/12 grid (of their bar, given a meter), rests absorbed
    into the previous note."""
    kept = []
    for i, (t, d, p) in enumerate(line):
        if i + 1 < len(line):
            t2, _, p2 = line[i + 1]
            if d < GRACE_BEATS and t2 - t < GRACE_BEATS and abs(p2 - p) <= 2:
                continue
        kept.append((t, d, p))
    grid: dict[float, tuple[int, float, float]] = {}   # rounded slot -> (pitch, length, slot)
    for t, d, p in kept:
        q = quantize(t, meter)
        key = round(q, 6)
        cur = grid.get(key)
        if cur is None or (p > cur[0] if highest else p < cur[0]):
            grid[key] = (p, d, q)
    slots = sorted(grid)
    out = []
    for i, key in enumerate(slots):
        p, d, q = grid[key]
        if i + 1 < len(slots):
            dur = grid[slots[i + 1]][2] - q
        else:
            dur = max(1, math.floor(d * GRID + 0.5)) / GRID
        out.append((key, round(dur, 6), p))
    return out


def build_line(notes: list[Note], shift_at: Callable[[float], int], meter: Meter, *, highest: bool, track: int | None,
               channel: int | None, method: str, confidence: float) -> Line:
    raw = skyline(notes, highest)
    shifted = [(t, d, p + shift_at(t)) for t, d, p in raw]
    clean = cleanup(shifted, highest=highest, meter=meter)
    return Line([c[0] for c in clean], [c[1] for c in clean], [int(c[2]) for c in clean],
                [meter.metric_class(c[0]) for c in clean], track, channel, method, round(confidence, 3))


def interval_entropy(pitches: list[int]) -> float:
    """Shannon entropy (bits) of the melody's intervals clipped to +-12 (repeats included)."""
    iv = [max(-12, min(12, b - a)) for a, b in zip(pitches, pitches[1:])]
    if not iv:
        return 0.0
    cnt = Counter(iv)
    n = len(iv)
    return round(-sum(c / n * math.log2(c / n) for c in cnt.values()), 4)


# --------------------------------------------------------------------------- lanes
def lanes(notes: list[Note]) -> dict[Lane, list[Note]]:
    out: dict[Lane, list[Note]] = {}
    for n in notes:
        if n[4] != DRUMS:
            out.setdefault((n[3], n[4]), []).append(n)
    return out


def _union(intervals: list[tuple[float, float]]) -> float:
    total, a0, b0 = 0.0, None, None
    for a, b in sorted(intervals):
        if b0 is None or a > b0:
            if b0 is not None:
                total += b0 - a0
            a0, b0 = a, b
        else:
            b0 = max(b0, b)
    if b0 is not None:
        total += b0 - a0
    return total


def lane_stats(lane: Lane, notes: list[Note], span: float, shift_at: Callable[[float], int]) -> LaneStats:
    # Polyphony = share of onsets that start a chord (>= 3 notes within 1/48 beat). Two-note
    # onsets are not counted: fan files often double the tune in thirds or octaves, and the
    # skyline takes care of that; strummed or comped parts start chords.
    ons = Counter(round(n[0] * 48) for n in notes)
    poly = sum(1 for c in ons.values() if c >= 3) / len(ons)
    sky = [p + shift_at(t) for t, _, p in skyline(notes)]
    ints = [abs(b - a) for a, b in zip(sky, sky[1:])]
    return LaneStats(
        lane=lane, n=len(notes),
        occupation=_union([(n[0], n[0] + n[1]) for n in notes]) / max(span, 1e-6),
        polyphony=poly,
        mean_pitch=sum(n[2] + shift_at(n[0]) for n in notes) / len(notes),
        mean_abs_interval=sum(ints) / len(ints) if ints else 0.0,
        mean_velocity=sum(n[5] for n in notes) / len(notes))


def _track_notes(notes: list[Note], track: int) -> list[Note]:
    return [n for n in notes if n[3] == track and n[4] != DRUMS]


def _main_channel(notes: list[Note]) -> int | None:
    return Counter(n[4] for n in notes).most_common(1)[0][0] if notes else None


def _named(features: dict | None, roles: tuple[str, ...], name_only: bool = True) -> list[dict]:
    out = []
    for t in (features or {}).get("tracks") or ():
        if t.get("is_drum") or t.get("role") not in roles:
            continue
        if name_only and t.get("role_src") not in (None, "name"):
            continue
        out.append(t)
    return sorted(out, key=lambda t: (-int(t.get("n_notes", 0)), int(t["index"])))


def lead_track_hint(features: dict | None) -> int | None:
    """Track to hand PatternPrep as ``LeadVocalTrack``: named vocal/melody, then lyric
    timing, then a named lead (the same order the melody selection uses)."""
    named = _named(features, ("vocal", "melody"))
    if named:
        return int(named[0]["index"])
    if (features or {}).get("lyric_melody_track") is not None:
        return int(features["lyric_melody_track"])
    named = _named(features, ("lead",))
    return int(named[0]["index"]) if named else None


def classify(stats: list[LaneStats], programs: dict[int, int]) -> list[tuple[float, LaneStats]]:
    """Heuristic melody scores of the lanes passing the gates, best first."""
    vel = [s.mean_velocity for s in stats]
    mu = sum(vel) / len(vel) if vel else 0.0
    sd = math.sqrt(sum((v - mu) ** 2 for v in vel) / len(vel)) if vel else 0.0
    scored = []
    for s in stats:
        if (s.n < CLASSIFIER_MIN_NOTES or s.occupation < 0.25 or s.polyphony > CLASSIFIER_MAX_POLY
                or s.mean_pitch < CLASSIFIER_MIN_PITCH):
            continue
        z = (s.mean_velocity - mu) / sd if sd > 1e-9 else 0.0
        score = (1.5 * s.occupation - 3.0 * s.polyphony + (1.0 if 55 <= s.mean_pitch <= 80 else 0.0)
                 - 0.4 * max(0.0, s.mean_abs_interval - 4.0)
                 + (0.5 if programs.get(s.lane[0]) in MELODIC_PROGRAMS else 0.0) + 0.3 * z)
        scored.append((score, s))
    scored.sort(key=lambda x: (-x[0], x[1].lane))
    return scored


def select_melody(notes: list[Note], features: dict | None, shift_at: Callable[[float], int],
                  meter: Meter) -> Line | None:
    pitched = [n for n in notes if n[4] != DRUMS]
    if not pitched:
        return None
    for tracks, method, conf in (
            ([int(t["index"]) for t in _named(features, ("vocal", "melody"))], "name", 0.9),
            ([] if (features or {}).get("lyric_melody_track") is None else [int(features["lyric_melody_track"])],
             "lyric_timing", min(0.95, 0.6 + 0.4 * float((features or {}).get("lyric_f1") or 0.5))),
            ([int(t["index"]) for t in _named(features, ("lead",))], "name", 0.75)):
        for track in tracks:
            tn = _track_notes(pitched, track)
            if len(skyline(tn)) >= NAME_MIN_NOTES:
                return build_line(tn, shift_at, meter, highest=True, track=track, channel=_main_channel(tn),
                                  method=method, confidence=conf)
    span = max(n[0] + n[1] for n in notes) - min(n[0] for n in notes)
    stats = [lane_stats(k, v, span, shift_at) for k, v in sorted(lanes(pitched).items())]
    programs = {int(t["index"]): int(t.get("program", -1)) for t in (features or {}).get("tracks") or ()}
    scored = classify(stats, programs)
    if scored:
        best = scored[0]
        margin = best[0] - scored[1][0] if len(scored) > 1 else 1.5
        lane = best[1].lane
        return build_line(lanes(pitched)[lane], shift_at, meter, highest=True, track=lane[0], channel=lane[1],
                          method="classifier", confidence=0.45 + min(0.35, 0.35 * margin / 1.5))
    high = [n for n in pitched if n[2] + shift_at(n[0]) >= 55]
    if not high:
        return None
    return build_line(high, shift_at, meter, highest=True, track=None, channel=None, method="skyline", confidence=0.2)


def select_bass(notes: list[Note], features: dict | None, shift_at: Callable[[float], int], meter: Meter,
                exclude: Lane | None = None) -> Line | None:
    """Lowest-note skyline of the bass lane: a track the sanitizer called ``bass`` (by name
    or GM program), else the lowest pitched lane (>= 32 notes, normalized mean < 60)."""
    lane_notes = bass_notes(notes, features, shift_at, exclude)
    if lane_notes is None:
        return None
    tn, track, channel = lane_notes
    return build_line(tn, shift_at, meter, highest=False, track=track, channel=channel, method="bass", confidence=1.0)


def bass_notes(notes: list[Note], features: dict | None, shift_at: Callable[[float], int] | None = None,
               exclude: Lane | None = None) -> tuple[list[Note], int, int | None] | None:
    pitched = [n for n in notes if n[4] != DRUMS]
    for t in _named(features, ("bass",), name_only=False):
        tn = _track_notes(pitched, int(t["index"]))
        if len(tn) >= NAME_MIN_NOTES and (exclude is None or exclude[0] != int(t["index"])):
            return tn, int(t["index"]), _main_channel(tn)
    shift = shift_at or (lambda _b: 0)
    best = None
    for lane, ln in sorted(lanes(pitched).items()):
        if len(ln) < NAME_MIN_NOTES or lane == exclude:
            continue
        mean = sum(n[2] + shift(n[0]) for n in ln) / len(ln)
        if best is None or mean < best[0] - 1e-9:
            best = (mean, lane, ln)
    if best is None or (shift_at is not None and best[0] >= 60):
        return None
    return best[2], best[1][0], best[1][1]
