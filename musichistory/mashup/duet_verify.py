"""Measurements on a rendered duet loop (no planned value is trusted).

* ``loop_key``: key of the decoded loop (``verify.segment_key``) against the root's (exact /
  relative / ...), for the whole loop and per duet run.
* ``loop_beats``: beat_this on the decoded loop against the planned grid (``verify.beat_agreement``).
* ``voice_beat_error``: per song, its vocal's onsets in the source window mapped through the
  time map against the onsets found in its rendered lead track (median |error| of pairs within
  60 ms), as ``verify.changeover_beat_error``.
* ``chords``: per run, the instrumental's half-bar chords read from the rendered instrumental
  bus (its harmonic part, HPSS, as ``analysis`` reads them) against the planned (exported) chords; the vocals' chord-tone share over them
  (``verify.slot_shares``) against each song's vocal over its own accompaniment (``native``).
* ``vocal_presence``: per second of the loop, the songs whose rendered lead track (before
  mixing) is within ``PRESENCE_DB`` of its own loud level (95th percentile of its per-second
  RMS while its window sounds): how many vocals are heard each second, against the plan (two,
  three in handoffs).
* ``seam``: the decoded file's length against the rendered loop, and the wrap (last sample ->
  first) checked like any join (``verify.clicks`` on the loop rotated by half), plus the
  sample step across the wrap against the 99.9th percentile of all sample steps.
* ``levels``: per run, the lead bus against the instrumental bus (RMS dB).
"""

from __future__ import annotations

import numpy as np

from ..paths.audio import chord_labels
from . import verify
from .chain import beat_agreement
from .duet_export import frame_offset, lead_element
from .duet_render import DuetTimeline

SR_A = verify.SR_A
PRESENCE_DB = 20.0


def _db(x: float) -> float:
    return float(20 * np.log10(max(float(x), 1e-12)))


def loop_key(tl: DuetTimeline, mono: np.ndarray) -> dict:
    root = tl.plan.tracks[0].key_name
    heard = verify.segment_key(mono, 0.0, tl.seconds)
    runs = []
    for r in tl.plan.runs:
        if r.kind != "duet":
            continue
        t0, t1 = tl.bar_time(r.start), tl.bar_time(r.end)
        h = verify.segment_key(mono, t0, t1)
        runs.append({"pair": r.pair, "heard": h, "relation": verify.key_relation(h, root)})
    return {"declared": root, "heard": heard, "relation": verify.key_relation(heard, root), "duets": runs}


def loop_beats(tl: DuetTimeline, dec: np.ndarray, sr: int) -> dict:
    detected, method = verify.mix_beats(None, dec, sr)
    res = verify.beat_agreement(tl.grid, detected, 0.0, tl.seconds)
    res["tracker"] = method
    res["bpm_planned"] = round(60.0 / float(np.median(np.diff(tl.grid))), 2)
    return res


def voice_beat_error(tl: DuetTimeline, song: int, track: np.ndarray, sr: int, src_lead: np.ndarray,
                     src_sr: int) -> dict:
    el = lead_element(tl, song)
    T = tl.seconds
    on_src = verify.onsets(verify._mono(src_lead, src_sr))
    expected = []
    for p in el.pieces:
        sel = on_src[(on_src >= p.src_times[0]) & (on_src < p.src_times[-1])]
        expected.append(np.interp(sel, p.src_times, p.out_times))
    exp = np.concatenate(expected) if expected else np.zeros(0)
    a, d = el.ramp_in[1], el.ramp_out[0]            # full-gain part of the window only
    exp = exp[(exp >= a) & (exp < d)] % T
    on_mix = verify.onsets(verify._mono(track, sr))
    if not len(exp) or not len(on_mix):
        return {"beat_error_ms": None, "onsets_matched": 0, "onsets": int(len(exp))}
    diff = np.array([on_mix[np.argmin(np.abs(on_mix - e))] - e for e in exp])
    ok = np.abs(diff) <= 0.06
    return {"beat_error_ms": round(float(np.median(np.abs(diff[ok]))) * 1000, 1) if ok.any() else None,
            "bias_ms": round(float(np.median(diff[ok])) * 1000, 1) if ok.any() else None,
            "onsets_matched": int(ok.sum()), "onsets": int(len(exp))}


def _slot_spans(tl: DuetTimeline, o: int, k: int) -> list[tuple[float, float, float]]:
    """(t0, t1, start beat) of the k equal slots of mix bar ``o``."""
    bpb = tl.bpb
    return [(float(tl.beat_time(o * bpb + i * bpb / k)), float(tl.beat_time(o * bpb + (i + 1) * bpb / k)),
             o * bpb + i * bpb / k) for i in range(k)]


def chords(tl: DuetTimeline, entry_chords: list, bed: np.ndarray, lead: np.ndarray, sr: int,
           native: dict[int, float | None]) -> dict:
    """Per run: rendered instrumental chords vs planned, and the vocals' chord-tone share."""
    plan = tl.plan
    b = verify._mono(bed, sr)
    ld = verify._mono(lead, sr)
    import librosa

    b_h, _ = librosa.effects.hpss(b)                     # harmonic part, as the analysis reads chords
    bc, lc, lr = verify._chroma(b_h), verify._chroma(ld), verify._rms(ld)
    planned = {}
    for a, e, root, q, _ in entry_chords:
        planned[(a, e)] = root * 2 + (1 if q == "min" else 0)
    off = frame_offset(tl)

    def planned_at(beat: float) -> int:
        for (a, e), lab in planned.items():
            if a - 1e-9 <= beat < e - 1e-9:
                return lab
        return -1

    runs = []
    for r in plan.runs:
        spans = []
        for o in range(r.start, r.end):
            spans += _slot_spans(tl, o, 2 if tl.bpb % 2 == 0 and tl.bpb >= 4 else 1)
        cols = [bc[:, verify._frames(t0, t1, bc.shape[1])].sum(axis=1) for t0, t1, _ in spans]
        heard = chord_labels(np.stack(cols, axis=1)) if cols else []
        agree = []
        for (t0, t1, beat), lab in zip(spans, heard):
            want = planned_at(beat + 1e-6)
            if want < 0 or lab < 0:
                continue
            want_h = ((want // 2 + off) % 12) * 2 + want % 2       # back from the frame to the root's key
            agree.append(beat_agreement(lab, want_h))
        shares = verify.slot_shares(lc, lr, bc, [(t0, t1) for t0, t1, _ in spans])
        vals = [x for x in shares if x is not None]
        runs.append({"kind": r.kind, "pair": r.pair, "chord_match_planned": r.chord_match,
                     "bed_chords_as_planned": round(float(np.mean(agree)), 3) if agree else None,
                     "vocal_chord_tone_share": round(float(np.mean(vals)), 3) if vals else None})
    return {"runs": runs, "native_chord_tone_share": native}


def native_share(track, src_lead: np.ndarray, src_back: np.ndarray, sr: int, bars: list[int]) -> float | None:
    """A song's lead over its own accompaniment in the source bars its window uses."""
    sl, sb = verify._mono(src_lead, sr), verify._mono(src_back, sr)
    spans = []
    for b in sorted(set(bars)):
        spans += verify.half_spans(track.bar_times(b), track.halves)
    sh = verify.slot_shares(verify._chroma(sl), verify._rms(sl), verify._chroma(sb), spans)
    vals = [x for x in sh if x is not None]
    return round(float(np.mean(vals)), 3) if vals else None


def vocal_presence(tl: DuetTimeline, leads: dict[int, np.ndarray], sr: int) -> dict:
    plan = tl.plan
    n_sec = int(np.floor(tl.seconds))
    per = {}
    planned = np.zeros(n_sec, dtype=int)
    for j, y in leads.items():
        m = y.mean(axis=1)
        rms = np.array([np.sqrt(np.mean(m[s * sr:(s + 1) * sr].astype(np.float64) ** 2)) for s in range(n_sec)])
        el = lead_element(tl, j)
        mids = np.arange(n_sec) + 0.5
        k = np.floor((mids - el.ramp_in[0]) / tl.seconds)
        u = mids - k * tl.seconds
        inside = (u >= el.ramp_in[0]) & (u < el.ramp_out[1])
        planned += inside.astype(int)
        db = 20 * np.log10(np.maximum(rms, 1e-12))
        loud = float(np.percentile(db[inside], 95)) if inside.any() else -120.0
        per[j] = db > loud - PRESENCE_DB
    count = np.sum(np.stack(list(per.values())), axis=0) if per else np.zeros(n_sec, dtype=int)
    low = [int(s) for s in np.flatnonzero(count < 2)]
    return {"seconds": n_sec, "planned_min": int(planned.min()) if n_sec else 0,
            "heard_histogram": {str(c): int(np.sum(count == c)) for c in range(0, max(4, int(count.max()) + 1))},
            "share_two_or_more": round(float(np.mean(count >= 2)), 3) if n_sec else None,
            "seconds_under_two": low,
            "per_song_share": {plan.tracks[j].work_id: round(float(np.mean(v[np.asarray(planned) > 0])), 3)
                               for j, v in per.items()}}


def seam(tl: DuetTimeline, dec: np.ndarray, sr: int, rendered_len: int) -> dict:
    n = len(dec)
    half = n // 2
    rot = np.roll(dec, half, axis=0)                 # the wrap now sits at sample n - half
    t_seam = (n - half) / sr
    grid = np.sort((tl.grid[:-1] + half / sr) % tl.seconds)
    c = verify.clicks(rot, sr, [t_seam], grid, tl.bpb)
    x = dec.mean(axis=1).astype(np.float64)
    steps = np.abs(np.diff(x))
    jump = abs(x[0] - x[-1])
    return {"decoded_samples": int(n), "rendered_samples": int(rendered_len), "length_delta": int(n - rendered_len),
            "click_ratio": c["worst_ratio"], "click": bool(c["clicks"]),
            "wrap_step": round(float(jump), 5), "p999_step": round(float(np.percentile(steps, 99.9)), 5),
            "wrap_step_ratio": round(float(jump / max(np.percentile(steps, 99.9), 1e-9)), 3)}


def levels(tl: DuetTimeline, bed: np.ndarray, lead: np.ndarray, sr: int) -> list[dict]:
    out = []
    for r in tl.plan.runs:
        a, b = int(tl.bar_time(r.start) * sr), int(tl.bar_time(r.end) * sr)
        rb = np.sqrt(np.mean(bed[a:b].astype(np.float64) ** 2))
        rl = np.sqrt(np.mean(lead[a:b].astype(np.float64) ** 2))
        out.append({"kind": r.kind, "pair": r.pair, "lead_minus_bed_db": round(_db(rl) - _db(rb), 1)})
    return out
