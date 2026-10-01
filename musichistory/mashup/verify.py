"""Measurements on a rendered mix (no planned value is trusted).

* ``chord_tone_share``: per changeover bar, the share of the lead bus's chroma energy that
  falls on the tones of the backing bus's chord in that half bar (chords read from the
  rendered backing as in ``tracks.Track.bar_chords``), compared with the same lead over its own
  accompaniment in the source recording (``native``). A matched changeover keeps the share
  near the native one.
* ``beat_error_ms``: vocal onsets (librosa onset detection on the lead stem) of the source
  window, mapped through the time map, against the onsets found in the rendered lead bus:
  median absolute difference of the matched pairs (within 60 ms) - how exactly the warped vocal
  keeps its place on the backing's beat grid.
* ``mix_beats``: beat_this on the decoded mix vs the planned beat grid per segment: every
  detected beat's distance to the nearest planned beat (median ms, share within 50 ms; a
  tracker that counts half time still lands on the grid), plus the tempo it hears.
* ``key``: key of each segment of the decoded mix (chroma profiles + chord function, as
  ``paths.audio``) vs the segment's declared key (exact / relative / other).
* ``peak``: decoded sample peak (dBFS) and samples at full scale; ``clicks``: at every piece
  join and envelope boundary, the largest second difference within 3 ms against the
  surrounding 250 ms and against the same beat of the neighbouring bars (both ratios >
  ``CLICK_RATIO`` = a click).
"""

from __future__ import annotations

import numpy as np

from ..paths.audio import chord_labels, key_scores, index_key
from ..identity.key import key_name
from .chain import triad
from .timeline import Timeline

SR_A = 22050
HOP = 512
CLICK_RATIO = 3.0


def _mono(y: np.ndarray, sr: int, target: int = SR_A) -> np.ndarray:
    import librosa

    m = y.mean(axis=1) if y.ndim == 2 else y
    return librosa.resample(m.astype(np.float32), orig_sr=sr, target_sr=target) if sr != target else m


def _chroma(y: np.ndarray) -> np.ndarray:
    import librosa

    return librosa.feature.chroma_cqt(y=y, sr=SR_A, hop_length=HOP)


def _rms(y: np.ndarray) -> np.ndarray:
    import librosa

    return librosa.feature.rms(y=y, hop_length=HOP)[0]


def tone_share(lead_chroma: np.ndarray, lead_rms: np.ndarray, label: int) -> float | None:
    """Energy-weighted share of the lead's chroma on the chord's three tones."""
    if label < 0 or lead_chroma.shape[1] == 0:
        return None
    tones = list(triad(label))
    w = lead_rms[: lead_chroma.shape[1]]
    if w.sum() <= 1e-9:
        return None
    c = lead_chroma / np.maximum(lead_chroma.sum(axis=0, keepdims=True), 1e-9)
    return float(np.sum(c[tones].sum(axis=0) * w) / np.sum(w))


def _frames(t0: float, t1: float, n: int) -> slice:
    a = int(t0 * SR_A / HOP)
    return slice(max(0, a), max(a + 1, min(n, int(t1 * SR_A / HOP))))


def slot_shares(lead_c, lead_r, back_c, spans) -> list[float | None]:
    """Per (t0, t1) half-bar span: the lead's chord-tone share over the backing's chord."""
    cols, sl = [], []
    for t0, t1 in spans:
        s = _frames(t0, t1, back_c.shape[1])
        sl.append(s)
        cols.append(back_c[:, s].sum(axis=1))
    labels = chord_labels(np.stack(cols, axis=1)) if cols else []
    return [tone_share(lead_c[:, s], lead_r[s], lab) for s, lab in zip(sl, labels)]


def half_spans(times: np.ndarray, halves) -> list[tuple[float, float]]:
    return [(float(times[a]), float(times[b])) for a, b in halves]


def changeover_tones(tl: Timeline, hop, lead_bus: np.ndarray, back_bus: np.ndarray, sr: int,
                     src_lead: np.ndarray, src_back: np.ndarray, src_sr: int) -> dict:
    """Chord-tone share of the lead per changeover bar in the mix vs in its own song."""
    track_b = tl.plan.tracks[hop.b]
    halves = track_b.halves
    bpb = tl.bpb
    lead = _mono(lead_bus, sr)
    back = _mono(back_bus, sr)
    lc, lr, bc = _chroma(lead), _rms(lead), _chroma(back)
    spans_mix, spans_src = [], []
    for k in range(hop.bars):
        o = hop.out_start + k
        spans_mix += half_spans(tl.grid[o * bpb:(o + 1) * bpb + 1], halves)
        spans_src += half_spans(track_b.bar_times(hop.b0 + k), halves)
    mix_sh = slot_shares(lc, lr, bc, spans_mix)
    sl = _mono(src_lead, src_sr)
    sb = _mono(src_back, src_sr)
    nat_sh = slot_shares(_chroma(sl), _rms(sl), _chroma(sb), spans_src)

    def mean(v):
        v = [x for x in v if x is not None]
        return round(float(np.mean(v)), 3) if v else None

    k = len(halves)
    per_bar = [mean(mix_sh[i * k:(i + 1) * k]) for i in range(hop.bars)]
    return {"mix": mean(mix_sh), "native": mean(nat_sh), "per_bar": per_bar}


def onsets(y: np.ndarray) -> np.ndarray:
    import librosa

    return librosa.onset.onset_detect(y=y, sr=SR_A, hop_length=256, units="time", backtrack=False)


def source_to_mix(tl: Timeline, song: int, t: np.ndarray) -> np.ndarray:
    out = np.full(len(t), np.nan)
    for p in tl.song_pieces(song):
        if not p.warped:
            continue
        sel = (t >= p.src_times[0]) & (t < p.src_times[-1]) & np.isnan(out)
        out[sel] = np.interp(t[sel], p.src_times, p.out_times)
    return out


def changeover_beat_error(tl: Timeline, hop, lead_bus: np.ndarray, sr: int, src_lead: np.ndarray,
                          src_sr: int) -> dict:
    bpb = tl.bpb
    t0 = float(tl.grid[hop.out_start * bpb])
    t1 = float(tl.grid[(hop.out_start + hop.bars) * bpb])
    track_b = tl.plan.tracks[hop.b]
    s0 = track_b.bar_start(hop.b0)
    s1 = track_b.bar_times(hop.b0 + hop.bars - 1)[-1]
    src = _mono(src_lead, src_sr)
    on_src = onsets(src)
    on_src = on_src[(on_src >= s0) & (on_src < s1)]
    expected = source_to_mix(tl, hop.b, on_src)
    expected = expected[np.isfinite(expected)]
    mix = _mono(lead_bus, sr)
    on_mix = onsets(mix)
    on_mix = on_mix[(on_mix >= t0 - 0.1) & (on_mix < t1 + 0.1)]
    if not len(expected) or not len(on_mix):
        return {"beat_error_ms": None, "onsets_matched": 0, "onsets": int(len(expected))}
    d = np.array([on_mix[np.argmin(np.abs(on_mix - e))] - e for e in expected])
    ok = np.abs(d) <= 0.06
    return {"beat_error_ms": round(float(np.median(np.abs(d[ok]))) * 1000, 1) if ok.any() else None,
            "bias_ms": round(float(np.median(d[ok])) * 1000, 1) if ok.any() else None,
            "onsets_matched": int(ok.sum()), "onsets": int(len(expected))}


def mix_beats(tl: Timeline, mix: np.ndarray, sr: int) -> tuple[np.ndarray, str]:
    from .analysis import track_beats

    beats, _, method = track_beats(_mono(mix, sr), SR_A)
    return beats, method


def beat_agreement(planned: np.ndarray, detected: np.ndarray, t0: float, t1: float) -> dict:
    p = planned[(planned >= t0) & (planned < t1)]
    det = detected[(detected >= t0 - 0.2) & (detected < t1 + 0.2)]
    if len(p) == 0 or len(det) < 2:
        return {"median_ms": None, "within_50ms": None, "bpm_heard": None}
    inside = det[(det >= t0) & (det < t1)]
    if len(inside) == 0:
        return {"median_ms": None, "within_50ms": None, "bpm_heard": None}
    err = np.array([np.min(np.abs(planned - x)) for x in inside])
    bpm = 60.0 / float(np.median(np.diff(inside))) if len(inside) > 2 else None
    return {"median_ms": round(float(np.median(err)) * 1000, 1), "within_50ms": round(float(np.mean(err <= 0.05)), 3),
            "bpm_heard": round(bpm, 2) if bpm else None}


def segment_key(mix_mono: np.ndarray, t0: float, t1: float) -> str | None:
    import librosa

    y = mix_mono[int(t0 * SR_A):int(t1 * SR_A)]
    if len(y) < SR_A:
        return None
    y_h, _ = librosa.effects.hpss(y)
    c = librosa.feature.chroma_cqt(y=y_h, sr=SR_A, hop_length=HOP)
    hist = c.mean(axis=1)
    labels = chord_labels(c[:, ::4])
    ch = np.bincount([x for x in labels if x >= 0], minlength=24)
    k = int(np.argmax(key_scores(hist / max(hist.sum(), 1e-9), ch)))
    t, m = index_key(k)
    return key_name(t, m)


def key_relation(heard: str | None, declared: str) -> str | None:
    if heard is None:
        return None
    if heard == declared:
        return "exact"
    from .tracks import parse_key
    from ..paths.identity import frame_shift

    a, b = parse_key(heard), parse_key(declared)
    if frame_shift(*a) == frame_shift(*b):
        return "relative"
    if a[0] == b[0]:
        return "parallel"
    if (a[0] - b[0]) % 12 in (5, 7) and a[1] == b[1]:
        return "fifth"
    return "other"


def _local_peak(d2: np.ndarray, i: int, w: int) -> float | None:
    if i - w < 0 or i + w >= len(d2):
        return None
    return float(d2[i - w:i + w].max())


def clicks(mix: np.ndarray, sr: int, times: list[float], grid: np.ndarray | None = None, bpb: int = 4) -> dict:
    """Sample-domain discontinuity at each boundary time: the largest second difference within
    3 ms of it, relative to (a) the 99.5th percentile of the surrounding 250 ms and (b) the
    largest within 15 ms of the same beat one and two bars earlier, or one and two bars later
    (on the mix's beat ``grid``), whichever side is larger: a drum hit on the downbeat of either
    song recurs every bar, a click does not. A boundary is a click when both ratios exceed
    ``CLICK_RATIO``."""
    x = mix.mean(axis=1) if mix.ndim == 2 else mix
    d2 = np.abs(np.diff(x.astype(np.float64), n=2))
    worst, bad = 0.0, []
    w, ctx, wr = int(0.003 * sr), int(0.25 * sr), int(0.015 * sr)
    for t in times:
        i = int(t * sr)
        if i - ctx < 0 or i + ctx >= len(d2):
            continue
        local = float(d2[i - w:i + w].max())
        around = np.r_[d2[i - ctx:i - 2 * w], d2[i + 2 * w:i + ctx]]
        r_ctx = local / (float(np.percentile(around, 99.5)) + 1e-9)
        r_bar = r_ctx
        if grid is not None and len(grid) > 1:
            k = int(np.argmin(np.abs(grid - t)))
            off = t - float(grid[k])
            sides = []
            for ms in ((-2, -1), (1, 2)):
                v = [x for m in ms if 0 <= k + m * bpb < len(grid)
                     and (x := _local_peak(d2, int((grid[k + m * bpb] + off) * sr), wr)) is not None]
                if v:
                    sides.append(float(np.median(v)))
            if sides:
                r_bar = local / (max(sides) + 1e-9)
        r = min(r_ctx, r_bar)
        worst = max(worst, r)
        if r > CLICK_RATIO:
            bad.append(round(t, 3))
    return {"checked": len(times), "worst_ratio": round(worst, 2), "clicks": bad}


def peak(mix: np.ndarray) -> dict:
    a = np.abs(mix)
    pk = float(a.max()) if a.size else 0.0
    return {"peak_dbfs": round(20 * np.log10(max(pk, 1e-9)), 2), "full_scale_samples": int(np.sum(a >= 0.999))}
