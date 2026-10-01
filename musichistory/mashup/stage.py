"""``python -m musichistory mashup [--path ID ...] [--all] [--stems-only]``: every featured path
(``data/audio/renders/paths.json``) as one continuous mashup mix, each song's vocal carried over
the previous song's instrumental, matched in key, tempo and chord progression (DESIGN §14).

1. **Stems** (``stems.py``): Resonance-2's offline Demucs separation of every song of the
   chosen paths into ``data/audio/stems/<work_id>/`` (resumable). ``--all`` separates every
   preview in ``data/audio`` instead (about 16 s each on the GPU, ~74 MB of float WAV each).
2. **Analysis** (``analysis.py``, cached per song): beat_this beats/downbeats, half-bar chords
   of the accompaniment, the vocal's pitch track and activity.
3. **Plan** (``chain.py``): full -> changeover (``--changeover`` s, whole bars) -> morph
   (``--morph-bars``) -> ... -> full, with each changeover's vocal window, transposition and
   tempo octave chosen for the best bar-level chord agreement (``--min-match``; shortened when
   it is not reached).
4. **Render** (``timeline.py``, ``warp.py``, ``render.py``): beat-synchronous time maps with
   Rubber Band (formant-preserving), equal-power bar-aligned fades, loudness-normalized MP3 in
   ``data/audio/mashups/<path id>/mix.mp3``.
5. **Verify** (``verify.py``) on the rendered mix and **export** ``data/audio/mashups/mashups.json``
   (``contract.py``) plus ``mashups_report.json`` with every measurement.

No lyrics anywhere: vocals are audio only, measured as pitch and energy.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np

from .. import config
from ..paths.ffmpeg import FfmpegNotFound, find_ffmpeg
from . import analysis, chain, contract, export, render, stems, timeline, tracks, verify


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--path", action="append", help="featured path id to render (repeatable; default all)")
    p.add_argument("--all", action="store_true", help="separate every preview in data/audio (stems only)")
    p.add_argument("--stems-only", action="store_true", help="separate and analyze, render nothing")
    p.add_argument("--force", action="store_true", help="separate and analyze again even when cached")
    p.add_argument("--changeover", type=float, default=20.0, help="changeover length in seconds (default 20)")
    p.add_argument("--morph-bars", type=int, default=2, help="morph length in bars (default 2)")
    p.add_argument("--full-bars", type=int, default=8, help="root song's opening phrase in bars (default 8)")
    p.add_argument("--final-bars", type=int, default=8, help="last song's closing phrase in bars (default 8)")
    p.add_argument("--min-match", type=float, default=0.75, help="chord agreement a changeover must reach")
    p.add_argument("--no-verify", action="store_true", help="skip the measurements on the rendered mix")


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def mashups_dir() -> Path:
    return config.DATA / "audio" / "mashups"


def _write_json(path: Path, doc, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=indent, ensure_ascii=False, separators=None if indent else (",", ":"))
                   + "\n", encoding="utf-8")
    os.replace(tmp, path)


def phrase_bars_for(identity: str | None) -> int:
    return 12 if identity and "12-bar" in identity else 8


def load_paths() -> list[dict]:
    doc = json.loads((config.DATA / "audio" / "renders" / "paths.json").read_text(encoding="utf-8"))
    return doc["paths"]


def preview(work_id: str) -> Path:
    return config.DATA / "audio" / work_id / "preview.mp3"


# --------------------------------------------------------------------------- one path
def render_path(p: dict, args: argparse.Namespace, ffmpeg: str) -> tuple[dict, dict]:
    t_start = time.monotonic()
    steps = p["steps"]
    docs = {s["work_id"]: analysis.analyze(s["work_id"], preview(s["work_id"])) for s in steps}
    base = [tracks.from_analysis(s["work_id"], docs[s["work_id"]], s["key"], title=s["title"], artist=s["artist"],
                                 year=int(s["year"])) for s in steps]
    plan = chain.plan_chain(base, co_seconds=args.changeover, morph_bars=args.morph_bars, full_bars=args.full_bars,
                            final_bars=args.final_bars, phrase_bars=phrase_bars_for(p.get("identity")),
                            min_match=args.min_match)
    tl = timeline.build(plan)
    dirs = {j: stems.stems_root() / t.work_id for j, t in enumerate(plan.tracks)}
    cache = render.StemCache(dirs)
    levels = {j: (docs[t.work_id].get("levels_db") or {}) for j, t in enumerate(plan.tracks)}
    lead, back, joins = render.render_buses(tl, cache, levels)
    mix = render.master(lead, back, tl)
    dst = mashups_dir() / contract.mix_file(p["id"])
    loud = render.write_mp3(ffmpeg, mix, dst)
    normalized = loud.pop("normalized")
    gain = float(10 ** (loud["gain_db"] / 20))
    render_s = time.monotonic() - t_start

    report: dict = {"id": p["id"], "render_seconds": round(render_s, 1), "loudness": loud,
                    "songs": [{"work_id": t.work_id, "title": t.title, "key": t.key_name, "bpm_counted": round(t.bpm, 2),
                               "regrid": t.factor, "bar_phase_shift": t.meta.get("phase_shift", 0),
                               "meter": t.meta.get("meter"), "lead": t.lead, "vocal_share": round(t.vocal_share, 3),
                               "bars": t.n_bars, "loop_period_bars": plan.periods[j]}
                              for j, t in enumerate(plan.tracks)],
                    "start_bar": plan.start_bar, "phrase_bars": plan.phrase_bars, "hops": []}
    hop_checks: dict[int, dict] = {}
    t_verify = time.monotonic()
    if not args.no_verify:
        import soundfile

        dec, dsr = soundfile.read(str(dst), dtype="float32", always_2d=True)
        mono = verify._mono(dec, dsr)
        detected, method = verify.mix_beats(tl, dec, dsr)
        report["peak"] = verify.peak(dec)
        bounds = sorted(set(joins) | {round(a, 4) for ivs in tl.envelopes.values() for iv in ivs for a in iv
                                      if 0.5 < a < tl.seconds - 0.5})
        report["clicks"] = verify.clicks(normalized, render.SR, bounds, tl.grid, tl.bpb)
        report["beat_tracker"] = method
        report["segments"] = []
        for kind, o0, o1, inst, voc in plan.segments():
            t0, t1 = float(tl.grid[o0 * tl.bpb]), float(tl.grid[o1 * tl.bpb])
            heard = verify.segment_key(mono, t0, t1)
            declared = plan.tracks[inst].key_name
            report["segments"].append({"kind": kind, "start": round(t0, 2), "end": round(t1, 2),
                                       "instrumental": plan.tracks[inst].title,
                                       "vocal": None if voc is None else plan.tracks[voc].title,
                                       "key_declared": declared, "key_heard": heard,
                                       "key_relation": verify.key_relation(heard, declared),
                                       "beats": verify.beat_agreement(tl.grid, detected, t0, t1)})
        for h in plan.hops:
            tb = plan.tracks[h.b]
            sl = cache.get(h.b, tb.lead)
            sb = cache.get(h.b, "bass") + cache.get(h.b, "other") if tb.lead == "vocals" else cache.get(h.b, "bass")
            g = lead * gain
            tones = verify.changeover_tones(tl, h, g, back * gain, render.SR, sl, sb, render.SR)
            be = verify.changeover_beat_error(tl, h, g, render.SR, sl, render.SR)
            hop_checks[h.out_start] = be
            t0 = float(tl.grid[h.out_start * tl.bpb])
            t1 = float(tl.grid[(h.out_start + h.bars) * tl.bpb])
            report["hops"].append({
                "from": plan.tracks[h.a].title, "to": tb.title, "start": round(t0, 2), "end": round(t1, 2),
                "seconds": round(t1 - t0, 2), "bars": h.bars, "target_bars": h.target_bars, "vocal_window_bar": h.b0,
                "instrumental_bars": h.a_bars, "shift": h.shift, "key_shift": h.key_shift,
                "tempo_ratio": round(plan.tracks[h.a].bpm / tb.bpm, 3), "chord_match": h.chord_match,
                "chord_match_per_bar": h.per_bar, "matched": h.ok, "vocal_cover": h.cover, "lead": h.lead,
                "extra_full_bars": h.extra, "chord_tone_share": tones, **be})
    report["verify_seconds"] = round(time.monotonic() - t_verify, 1)
    meta = [{"work_id": s["work_id"], "title": s["title"], "artist": s["artist"], "year": s["year"]} for s in steps]
    pitch = []
    for t in plan.tracks:
        if t.lead == "vocals":
            pitch.append((docs[t.work_id]["pitch"], float(docs[t.work_id]["pitch_hop"])))
        else:
            pitch.append(analysis.lead_pitch(t.work_id, t.lead))
    entry = export.path_entry(p["id"], p["title"], tl, meta, pitch, hop_checks, len(normalized) / render.SR)
    return entry, report


# --------------------------------------------------------------------------- run
def run(args: argparse.Namespace) -> int:
    t0 = time.monotonic()
    paths = load_paths()
    if args.path:
        unknown = set(args.path) - {p["id"] for p in paths}
        if unknown:
            _log(f"error: unknown path id(s): {', '.join(sorted(unknown))}")
            return 2
        paths = [p for p in paths if p["id"] in args.path]
    if args.all:
        previews = {d.name: d / "preview.mp3" for d in (config.DATA / "audio").iterdir()
                    if (d / "preview.mp3").is_file()}
    else:
        previews = {s["work_id"]: preview(s["work_id"]) for p in paths for s in p["steps"]}
    _log(f"stems: {len(previews)} previews")
    st = stems.separate_all(previews, force=args.force, log=_log)
    secs = list(st["seconds"].values())
    _log(f"stems: {st['done']} already done, {st['separated']} separated"
         + (f" ({np.mean(secs):.1f} s each, {sum(secs):.0f} s total)" if secs else "") + f", {len(st['failed'])} failed")
    for wid, e in st["failed"].items():
        _log(f"  failed {wid}: {e}")
    if args.stems_only or args.all:
        for wid, src in sorted(previews.items()):
            if wid not in st["failed"] and stems.is_done(src, stems.stems_root() / wid):
                analysis.analyze(wid, src, force=args.force)
        _log(f"analysis done in {time.monotonic() - t0:.0f}s")
        return 0 if not st["failed"] else 1
    try:
        ffmpeg = find_ffmpeg()
    except FfmpegNotFound as exc:
        _log(f"error: {exc}")
        return 2

    out_dir = mashups_dir()
    doc_path = out_dir / "mashups.json"
    old = {}
    if doc_path.is_file():
        try:
            old = {p["id"]: p for p in json.loads(doc_path.read_text(encoding="utf-8")).get("paths", [])}
        except ValueError:
            old = {}
    report_path = out_dir / "mashups_report.json"
    old_report = {}
    if report_path.is_file():
        try:
            old_report = {r["id"]: r for r in json.loads(report_path.read_text(encoding="utf-8")).get("paths", [])}
        except ValueError:
            old_report = {}
    entries, reports, failed = dict(old), dict(old_report), {}
    for p in paths:
        _log(f"\n{p['id']}: {p['title']}")
        try:
            entry, rep = render_path(p, args, ffmpeg)
        except Exception as exc:  # one path must not stop the others
            import traceback

            failed[p["id"]] = f"{type(exc).__name__}: {exc}"
            _log(f"  FAILED: {failed[p['id']]}")
            _log(traceback.format_exc())
            continue
        entries[p["id"]] = entry
        reports[p["id"]] = rep
        for s in entry["segments"]:
            extra = ""
            if s["kind"] == "changeover":
                extra = (f" shift {s['vocal_shift_semitones']:+d}, tempo x{s['vocal_tempo_ratio']:.3f}, chord match "
                         f"{s['chord_match']:.2f}, beat error {s['beat_error_ms']} ms")
            _log(f"  {s['start']:7.2f}-{s['end']:7.2f} {s['kind']:10s} {s['instrumental']}"
                 f"{'' if s['vocal'] in (None, s['instrumental']) else ' + ' + s['vocal']} {s['key']}"
                 f" {s['bpm_start']:.1f}->{s['bpm']:.1f} BPM{extra}")
        for h in rep["hops"]:
            flag = "" if h["matched"] else "  (NOT MATCHED: best chord agreement under --min-match)"
            _log(f"  hop {h['from']} -> {h['to']}: {h['bars']}/{h['target_bars']} bars ({h['seconds']:.1f}s),"
                 f" chord match {h['chord_match']:.2f}, chord-tone share {h['chord_tone_share']['mix']} vs native"
                 f" {h['chord_tone_share']['native']}, beat error {h.get('beat_error_ms')} ms{flag}")
        _log(f"  {entry['seconds']:.1f}s mix, {rep['loudness']['lufs']} LUFS, TP {rep['loudness']['true_peak']} dBTP,"
             f" peak {rep.get('peak')}, clicks {rep.get('clicks', {}).get('clicks')}, render {rep['render_seconds']}s")
    order = [p["id"] for p in load_paths()]
    doc = {"version": contract.VERSION, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "frame": contract.FRAME, "paths": [entries[i] for i in order if i in entries]}
    errors = contract.validate(doc, out_dir)
    if errors:
        for e in errors[:40]:
            _log(f"contract error: {e}")
        return 1
    _write_json(doc_path, doc)
    _write_json(report_path, {"generated_at": doc["generated_at"], "paths": [reports[i] for i in order if i in reports],
                              "failed": failed}, indent=1)
    _log(f"\nwrote {doc_path} ({len(doc['paths'])} paths) and {report_path} in {time.monotonic() - t0:.0f}s")
    return 1 if failed else 0
