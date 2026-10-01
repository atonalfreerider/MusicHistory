"""One path's entry of ``mashups.json`` (see ``contract.py``) from its timeline.

**Phrase frame.** Mix bar 0 is the root song's start bar; mix bar ``o`` sits at phrase bar
``o mod phrase_bars``, so every beat of the mix has a phrase position. Each changeover lays
the next song's bars onto the previous song's bars where their chords agree, so the phrase
position of a chord is the same in every song as far as they match.

**Normalized frame.** Song 1's pitches are moved by its key's relative-normalization shift
(C major / A minor). Song j's vocal is heard transposed by ``shift_j`` into song j-1's key, so
its offset is ``F_j = F_{j-1} - shift_j`` (kept within an octave of 0): every song's melody
and chords land in the frame in which the mix actually lines them up.

**Melody.** The lead's pitch track (pYIN on the vocal stem; on the ``other`` stem for a song
without a sung vocal) sampled once per 1/16 beat of the mix wherever the song's lead is
audible: the mix time of each sample is mapped back to the source through the time map and the
median of the voiced pitch frames within the sample is taken (less the song's tuning
deviation, so every melody sits on the same semitone grid); unvoiced samples and wraps of the
phrase are ``null``; tracker slips (over 13 semitones from the median, lone octave jumps) are
dropped. **Chords**: the song's own half-bar chords per phrase slot, the most frequent one
where its vocal sits, else where its instrumental plays (gaps filled from its neighbouring bars).
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .timeline import Timeline

ROMAN = {0: "I", 1: "bII", 2: "II", 3: "bIII", 4: "III", 5: "IV", 6: "#IV", 7: "V", 8: "bVI", 9: "VI", 10: "bVII",
         11: "VII"}
STEP = 1 / 16
OUTLIER = 13.0          # semitones from the melody's median: a tracker slip, not singing
SPIKE = 7.0             # a lone point this far from both neighbours is an octave error
LEAD_STEMS = {"vocals", "other"}


def wrap_offset(f: int) -> int:
    f = int(f)
    while f > 6:
        f -= 12
    while f < -6:
        f += 12
    return f


def frame_offsets(tl: Timeline) -> list[int]:
    tracks = tl.plan.tracks
    offs = [wrap_offset(tracks[0].frame_shift)]
    for j in range(1, len(tracks)):
        f = offs[-1] - tl.shifts.get(j, 0)
        while f > 9:
            f -= 12
        while f < -9:
            f += 12
        offs.append(f)
    return offs


def roman(root: int, minor: bool, mode: str) -> str:
    tonic = 9 if mode == "minor" else 0
    r = ROMAN[(root - tonic) % 12]
    if minor:
        i = len(r) - len(r.lstrip("b#"))
        r = r[:i] + r[i:].lower()
    return r


def intervals_of(tl: Timeline, song: int, stems) -> list[list[float]]:
    ivs = sorted(iv for (s, st), v in tl.envelopes.items() if s == song and st in stems for iv in v)
    out: list[list[float]] = []
    for a, b in ivs:
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(a, 3), round(b, 3)] for a, b in out]


def melody(tl: Timeline, song: int, pitch: list, hop: float, offset: int, phrase_beats: float,
           tuning: float = 0.0) -> list:
    p = np.array([np.nan if v is None else float(v) for v in pitch], dtype=float)
    ivs = intervals_of(tl, song, LEAD_STEMS)
    idx = np.arange(len(tl.grid), dtype=float)
    pts: list[list] = []
    for a, b in ivs:
        q0 = np.ceil(float(np.interp(a, tl.grid, idx)) / STEP) * STEP
        q1 = float(np.interp(b, tl.grid, idx))
        qs = np.arange(q0, q1, STEP)
        if not len(qs):
            continue
        t_mid = np.interp(qs + STEP / 2, idx, tl.grid)
        t_lo = np.interp(qs, idx, tl.grid)
        t_hi = np.interp(qs + STEP, idx, tl.grid)
        s_lo, s_hi = tl.mix_to_source(song, t_lo), tl.mix_to_source(song, t_hi)
        s_mid = tl.mix_to_source(song, t_mid)
        if pts and pts[-1][1] is not None:
            pts.append([pts[-1][0], None])
        prev_pb = None
        for q, lo, hi, mid in zip(qs, s_lo, s_hi, s_mid):
            pb = round(float(q % phrase_beats), 4)
            if prev_pb is not None and pb < prev_pb and pts and pts[-1][1] is not None:
                pts.append([prev_pb, None])                       # the phrase wraps
            prev_pb = pb
            if not np.isfinite(mid):
                val = None
            else:
                i0 = int(np.floor(min(lo, hi) / hop)) if np.isfinite(lo) and np.isfinite(hi) else int(mid / hop)
                i1 = int(np.ceil(max(lo, hi) / hop)) if np.isfinite(lo) and np.isfinite(hi) else i0 + 1
                win = p[max(0, i0):max(i0 + 1, i1)]
                win = win[np.isfinite(win)]
                val = round(float(np.median(win)) - offset - tuning, 2) if len(win) >= max(1, (i1 - i0) // 2) else None
            if val is None and (not pts or pts[-1][1] is None):
                continue
            pts.append([pb, val])
    return despike(pts)


def despike(pts: list[list]) -> list[list]:
    """Drop pitch-tracker slips: values more than ``OUTLIER`` semitones from the melody's
    median, and single points more than ``SPIKE`` semitones from both voiced neighbours
    (octave errors); then collapse repeated breaks and trim breaks at the ends."""
    vals = [v for _, v in pts if v is not None]
    if vals:
        med = float(np.median(vals))
        pts = [[q, None if v is not None and abs(v - med) > OUTLIER else v] for q, v in pts]
    for i in range(1, len(pts) - 1):
        a, v, b = pts[i - 1][1], pts[i][1], pts[i + 1][1]
        if v is not None and a is not None and b is not None and abs(v - a) > SPIKE and abs(v - b) > SPIKE:
            pts[i][1] = None
    out: list[list] = []
    for q, v in pts:
        if v is None and (not out or out[-1][1] is None):
            continue
        out.append([q, v])
    while out and out[-1][1] is None:
        out.pop()
    return out


def chords(tl: Timeline, song: int, offset: int, phrase_bars: int) -> list[list]:
    tr = tl.plan.tracks[song]
    halves = tr.halves
    bpb = tl.bpb
    slots: dict[int, Counter] = {}
    first = None
    # the song's own chords where its melody sits (its vocal bars) first, then where its
    # instrumental plays, then its neighbouring bars
    for role in ("vocal", "inst"):
        found: dict[int, Counter] = {}
        for o, ob in enumerate(tl.plan.bars):
            ref = ob.vocal if role == "vocal" else ob.inst
            if ref is None or ref[0] != song:
                continue
            if first is None:
                first = (o, ref[1])
            for k, lab in enumerate(tr.bar_chords(ref[1])):
                if lab >= 0:
                    found.setdefault((o % phrase_bars) * len(halves) + k, Counter())[lab] += 1
        for key, c in found.items():
            slots.setdefault(key, c)
    if first is not None:
        o_f, s_f = first
        for q in range(phrase_bars):
            for k in range(len(halves)):
                key = q * len(halves) + k
                if key in slots:
                    continue
                cands = sorted((abs(d), s_f + d) for d in range(-tr.n_bars, tr.n_bars)
                               if 0 <= s_f + d < tr.n_bars and (o_f + d) % phrase_bars == q)
                for _, s in cands:
                    lab = tr.bar_chords(s)[k] if k < len(tr.bar_chords(s)) else -1
                    if lab >= 0:
                        slots[key] = Counter({lab: 1})
                        break
    out: list[list] = []
    for key in sorted(slots):
        q, k = divmod(key, len(halves))
        a, b = halves[k]
        start, end = q * bpb + a, q * bpb + b
        lab = slots[key].most_common(1)[0][0]
        root = (lab // 2 - offset) % 12
        minor = bool(lab % 2)
        if out and out[-1][1] == start and out[-1][2] == root and out[-1][3] == ("min" if minor else "maj"):
            out[-1][1] = end
            continue
        out.append([start, end, root, "min" if minor else "maj", roman(root, minor, tr.mode)])
    return [[float(a), float(b), int(r), q, rn] for a, b, r, q, rn in out]


def segments(tl: Timeline, hop_checks: dict[int, dict]) -> list[dict]:
    tracks = tl.plan.tracks
    bpb = tl.bpb
    hops = {h.out_start: h for h in tl.plan.hops}
    out = []
    for kind, o0, o1, inst, voc in tl.plan.segments():
        g = tl.grid[o0 * bpb:o1 * bpb + 1]
        ibis = np.diff(g)
        seg = {"start": round(float(g[0]), 3), "end": round(float(g[-1]), 3), "kind": kind,
               "instrumental": tracks[inst].work_id, "vocal": tracks[voc].work_id if voc is not None else None,
               "key": tracks[inst].key_name, "bpm": round(60.0 / float(np.median(ibis)), 2),
               "bpm_start": round(60.0 / float(np.median(ibis)), 2), "vocal_shift_semitones": 0,
               "vocal_tempo_ratio": 1.0, "chord_match": None, "beat_error_ms": None}
        if kind == "changeover":
            h = hops[o0]
            src = np.concatenate([np.diff(tracks[voc].bar_times(h.b0 + k)) for k in range(h.bars)])
            seg["vocal_shift_semitones"] = int(h.shift)
            seg["vocal_tempo_ratio"] = round(float(np.sum(src) / np.sum(ibis)), 4)
            seg["chord_match"] = round(float(h.chord_match), 4)
            be = hop_checks.get(o0, {}).get("beat_error_ms")
            seg["beat_error_ms"] = None if be is None else float(be)
        elif kind == "morph":
            h = next(x for x in tl.plan.hops if x.b == inst)
            n_first = min(bpb, len(ibis))
            seg["bpm_start"] = round(60.0 / float(np.median(ibis[:n_first])), 2)
            seg["bpm"] = round(60.0 / float(np.median(ibis[-n_first:])), 2)
            seg["vocal_shift_semitones"] = int(h.shift)
            src = np.diff(tracks[inst].bar_times(tl.plan.bars[o0].inst[1]))
            seg["vocal_tempo_ratio"] = round(float(np.sum(src) / np.sum(ibis[:len(src)])), 4)
        out.append(seg)
    return out


def path_entry(path_id: str, title: str, tl: Timeline, songs_meta: list[dict], pitch_tracks: list[tuple[list, float]],
               hop_checks: dict[int, dict], seconds: float) -> dict:
    from .contract import mix_file

    bpb = tl.bpb
    pbars = tl.plan.phrase_bars
    phrase_beats = pbars * bpb
    offs = frame_offsets(tl)
    beats = [[round(float(t), 3), float(k % phrase_beats)] for k, t in enumerate(tl.grid) if t < seconds - 1e-6]
    songs = []
    for j, meta in enumerate(songs_meta):
        pitch, hop = pitch_tracks[j]
        songs.append({"work_id": meta["work_id"], "title": meta["title"], "artist": meta["artist"],
                      "year": int(meta["year"]), "step": j,
                      "melody": melody(tl, j, pitch, hop, offs[j], phrase_beats, tl.plan.tracks[j].tuning),
                      "chords": chords(tl, j, offs[j], pbars),
                      "vocal_audible": intervals_of(tl, j, LEAD_STEMS),
                      "instrumental_audible": intervals_of(tl, j, {"instruments", "drums", "bass"})})
    segs = segments(tl, hop_checks)
    segs[-1]["end"] = round(seconds, 3)
    return {"id": path_id, "title": title, "file": mix_file(path_id), "seconds": round(seconds, 3),
            "beats_per_bar": int(bpb), "phrase_beats": float(phrase_beats), "segments": segs, "beats": beats,
            "songs": songs}
