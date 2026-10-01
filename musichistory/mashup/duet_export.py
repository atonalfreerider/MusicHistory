"""One path's entry of ``duets.json`` (see ``duet_contract.py``) from its duet timeline.

**Normalized frame.** Everything is heard in the root's key, so one offset serves the whole
loop: ``F0`` = the root's relative-normalization shift (C major / A minor, within a tritone).
Song j's vocal is heard transposed by its ``shift``; a pitch ``p`` of its own is heard at
``p + shift`` and lands at ``p + shift - F0 - tuning`` in the frame (its tuning deviation
removed, so every melody sits on the root's semitone grid).

**Melody.** As ``export.melody``, but in mix beats of the loop (0 .. beats in the loop): the
song's lead pitch track sampled once per 1/16 beat wherever its vocal window sounds (fades
included), each sample mapped back to the source through the window's time map and the
median of its voiced frames taken; unvoiced samples and the window's wrap around the loop are
``null`` breaks; tracker slips are dropped (``export.despike``).

**Chords.** What the instrumental actually plays, per half-bar slot of every mix bar: the bed's
chords in duet runs, the borrowed instrumental's (transposed by its song's shift) in handoffs,
merged where consecutive slots agree.
"""

from __future__ import annotations

import numpy as np

from .chain import shift_label
from .duet_render import DuetTimeline
from .export import STEP, despike, roman, wrap_offset


def mix_to_source(tl: DuetTimeline, el, t: np.ndarray) -> np.ndarray:
    """Source seconds heard at unwrapped mix time(s) ``t`` from element ``el`` (NaN outside)."""
    t = np.atleast_1d(np.asarray(t, dtype=float))
    out = np.full(t.shape, np.nan)
    for p in el.pieces:
        sel = (t >= p.out_start) & (t < p.out_end)
        out[sel] = np.interp(t[sel], p.out_times, p.src_times)
    return out


def lead_element(tl: DuetTimeline, song: int):
    return next(e for e in tl.elements if e.role == "lead" and e.song == song)


def frame_offset(tl: DuetTimeline) -> int:
    return wrap_offset(tl.plan.tracks[0].frame_shift)


def melody(tl: DuetTimeline, song: int, pitch: list, hop: float) -> list:
    plan = tl.plan
    v = plan.voices[song]
    el = lead_element(tl, song)
    nb = tl.n_beats
    offset = frame_offset(tl) - v.shift
    tuning = plan.tracks[song].tuning
    p = np.array([np.nan if x is None else float(x) for x in pitch], dtype=float)
    qs = np.arange(v.start * tl.bpb, v.end * tl.bpb, STEP)
    t_lo, t_hi, t_mid = tl.beat_time(qs), tl.beat_time(qs + STEP), tl.beat_time(qs + STEP / 2)
    s_lo, s_hi, s_mid = mix_to_source(tl, el, t_lo), mix_to_source(tl, el, t_hi), mix_to_source(tl, el, t_mid)
    vals: list[float | None] = []
    for lo, hi, mid in zip(s_lo, s_hi, s_mid):
        if not np.isfinite(mid):
            vals.append(None)
            continue
        if np.isfinite(lo) and np.isfinite(hi):
            i0, i1 = int(np.floor(min(lo, hi) / hop)), int(np.ceil(max(lo, hi) / hop))
        else:
            i0 = int(mid / hop)
            i1 = i0 + 1
        win = p[max(0, i0):max(i0 + 1, i1)]
        win = win[np.isfinite(win)]
        vals.append(round(float(np.median(win)) - offset - tuning, 2) if len(win) >= max(1, (i1 - i0) // 2) else None)
    wrapped = np.round((qs % nb) / STEP).astype(int)
    order = np.argsort(wrapped, kind="stable")
    pts: list[list] = []
    prev = None
    for i in order:
        q = float(wrapped[i] * STEP)
        if prev is not None and wrapped[i] != prev + 1 and pts and pts[-1][1] is not None:
            pts.append([pts[-1][0], None])                  # the window's wrap around the loop
        prev = wrapped[i]
        val = vals[i]
        if val is None and (not pts or pts[-1][1] is None):
            continue
        pts.append([round(q, 4), val])
    return despike(pts)


def chords(tl: DuetTimeline) -> list[list]:
    plan = tl.plan
    bpb = tl.bpb
    f0 = frame_offset(tl)
    mode = plan.tracks[0].mode
    out: list[list] = []
    for o in range(plan.n_bars):
        song, src, shift = plan.inst_at(o)
        labs = plan.tracks[song].bar_chords(src)
        k = len(labs)
        for i, lab in enumerate(labs):
            if lab < 0:
                continue
            h = shift_label(lab, shift)
            root = (h // 2 - f0) % 12
            minor = bool(h % 2)
            a = o * bpb + i * bpb / k
            b = o * bpb + (i + 1) * bpb / k
            q = "min" if minor else "maj"
            if out and abs(out[-1][1] - a) < 1e-9 and out[-1][2] == root and out[-1][3] == q:
                out[-1][1] = b
                continue
            out.append([a, b, root, q, roman(root, minor, mode)])
    return [[float(a), float(b), int(r), q, rn] for a, b, r, q, rn in out]


def wrapped_intervals(tl: DuetTimeline, spans: list[tuple[float, float]]) -> list[list[float]]:
    """Unwrapped (t0, t1) spans folded into [0, seconds], merged and rounded."""
    T = tl.seconds
    ivs = []
    for a, b in spans:
        k = np.floor(a / T)
        a, b = a - k * T, b - k * T
        if b > T + 1e-9:
            ivs += [(a, T), (0.0, b - T)]
        else:
            ivs.append((a, b))
    out: list[list[float]] = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(a, 3), round(min(b, T), 3)] for a, b in out if b - a > 1e-6]


def tempo_ratio(tl: DuetTimeline, song: int) -> float:
    """Source seconds per mix second of the song's vocal (> 1: sped up), median over beats."""
    el = lead_element(tl, song)
    r = np.concatenate([np.diff(p.src_times) / np.diff(p.out_times) for p in el.pieces])
    return round(float(np.median(r)), 4)


def segments(tl: DuetTimeline) -> list[dict]:
    plan = tl.plan
    wid = [t.work_id for t in plan.tracks]
    out = []
    for r in plan.runs:
        out.append({"start": round(tl.bar_time(r.start), 3), "end": round(tl.bar_time(r.end), 3), "kind": r.kind,
                    "instrumental": wid[r.song], "vocals": [wid[r.vocals[0]], wid[r.vocals[1]]],
                    "entering": None if r.entering is None else wid[r.entering],
                    "leaving": None if r.leaving is None else wid[r.leaving],
                    "chord_match": float(r.chord_match)})
    return out


def path_entry(path_id: str, title: str, tl: DuetTimeline, songs_meta: list[dict],
               pitch_tracks: list[tuple[list, float]]) -> dict:
    from .duet_contract import loop_file

    plan = tl.plan
    root = plan.tracks[0]
    T = tl.seconds
    beats = [[round(float(tl.grid[k]), 3), float(k)] for k in range(tl.n_beats)]
    songs = []
    for j, meta in enumerate(songs_meta):
        v = plan.voices[j]
        pitch, hop = pitch_tracks[j]
        inst = [(tl.bar_time(r.start), tl.bar_time(r.end)) for r in plan.runs if r.song == j]
        songs.append({"work_id": meta["work_id"], "title": meta["title"], "artist": meta["artist"],
                      "year": int(meta["year"]), "step": j, "shift_semitones": int(v.shift),
                      "tempo_ratio": tempo_ratio(tl, j), "melody": melody(tl, j, pitch, hop),
                      "vocal_audible": wrapped_intervals(tl, [(tl.bar_time(v.start), tl.bar_time(v.end))]),
                      "instrumental_audible": wrapped_intervals(tl, inst)})
    segs = segments(tl)
    segs[-1]["end"] = round(T, 3)
    return {"id": path_id, "title": title, "file": loop_file(path_id), "seconds": round(T, 3), "loops": True,
            "root": root.work_id, "key": root.key_name, "bpm": round(60.0 / float(np.median(np.diff(tl.grid))), 2),
            "beats_per_bar": int(tl.bpb), "phrase_beats": float(plan.phrase_bars * tl.bpb), "segments": segs,
            "beats": beats, "chords": chords(tl), "songs": songs}
