"""``python -m musichistory duets [--path ID ...] [--pair S] [--handoff-bars N] [--no-verify]``:
every featured path (``data/audio/renders/paths.json``) as an unnarrated, seamless duet loop -
two sung melodies at all times, in the root song's key and tempo, over the root's instrumental,
handing off around the path and wrapping back to the start (DESIGN §16).

1. **Stems and analysis** are the ``mashup`` stage's (``data/audio/stems/<work_id>/``, cached);
   a song without them fails its path (run ``mashup`` first). Nothing here uses the GPU except
   beat_this for the verification, which follows ``CUDA_VISIBLE_DEVICES``.
2. **Plan** (``duet.py``): pairs of ``--pair`` seconds (whole bars of the root), handoffs of
   ``--handoff-bars`` bars centred on the pair boundaries, every vocal window and borrowed
   instrumental chosen for chord agreement with the bed.
3. **Render** (``duet_render.py``): circular, beat-synchronous Rubber Band time maps
   (formant-preserving), equal-power fades, loudness-normalized MP3 in
   ``data/audio/duets/<path id>/loop.mp3``.
4. **Verify** (``duet_verify.py``, unless ``--no-verify``) and **export**
   ``data/audio/duets/duets.json`` (``duet_contract.py``, validated before writing) plus
   ``duets_report.json`` with every measurement.

No lyrics anywhere: vocals are audio only, measured as pitch and energy.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .. import config
from ..paths.ffmpeg import FfmpegNotFound, find_ffmpeg
from . import analysis, duet, duet_contract, duet_export, duet_render, duet_verify, render, stems, tracks, verify
from .contract import FRAME
from .stage import _write_json, load_paths, phrase_bars_for, preview


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--path", action="append", help="featured path id to render (repeatable; default all)")
    p.add_argument("--pair", type=float, default=20.0, help="seconds per pair of vocals (default 20, whole bars)")
    p.add_argument("--handoff-bars", type=int, default=2, help="handoff length in bars (default 2)")
    p.add_argument("--no-verify", action="store_true", help="skip the measurements on the rendered loop")


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def duets_dir() -> Path:
    return config.DATA / "audio" / "duets"


# --------------------------------------------------------------------------- one path
def render_path(p: dict, args: argparse.Namespace, ffmpeg: str) -> tuple[dict, dict]:
    t_start = time.monotonic()
    steps = p["steps"]
    for s in steps:
        if not stems.is_done(preview(s["work_id"]), stems.stems_root() / s["work_id"]):
            raise FileNotFoundError(f"no stems for {s['work_id']} ({s['title']}): run the mashup stage first")
    docs = {s["work_id"]: analysis.analyze(s["work_id"], preview(s["work_id"])) for s in steps}
    base = [tracks.from_analysis(s["work_id"], docs[s["work_id"]], s["key"], title=s["title"], artist=s["artist"],
                                 year=int(s["year"])) for s in steps]
    plan = duet.plan_duet(base, pair_seconds=args.pair, handoff_bars=args.handoff_bars,
                          phrase_bars=phrase_bars_for(p.get("identity")))
    tl = duet_render.build(plan)
    cache = duet_render.DuetStems({j: stems.stems_root() / t.work_id for j, t in enumerate(plan.tracks)})
    levels = {j: (docs[t.work_id].get("levels_db") or {}) for j, t in enumerate(plan.tracks)}
    buses = duet_render.render_buses(tl, cache, levels)
    mix = buses.mix()
    dst = duets_dir() / duet_contract.loop_file(p["id"])
    loud = duet_render.write_loop_mp3(ffmpeg, mix, dst)
    normalized = loud.pop("normalized")
    gain = float(10 ** (loud["gain_db"] / 20))
    render_s = time.monotonic() - t_start

    meta = [{"work_id": s["work_id"], "title": s["title"], "artist": s["artist"], "year": s["year"]} for s in steps]
    pitch = []
    for t, v in zip(plan.tracks, plan.voices):
        if v.stem == "vocals":
            pitch.append((docs[t.work_id]["pitch"], float(docs[t.work_id]["pitch_hop"])))
        else:
            pitch.append(analysis.lead_pitch(t.work_id, v.stem))
    entry = duet_export.path_entry(p["id"], p["title"], tl, meta, pitch)

    root = plan.tracks[0]
    report: dict = {
        "id": p["id"], "render_seconds": round(render_s, 1), "loudness": loud, "seconds": entry["seconds"],
        "root": root.title, "key": root.key_name, "bpm": entry["bpm"], "pair_bars": plan.pair_bars,
        "handoff_bars": plan.handoff_bars, "loop_bars": plan.n_bars, "bed_start_bar": plan.params["start_bar"],
        "bed_loop_bars": list(plan.params["bed_loop"]), "pan": duet_render.PAN, "lead_rel_db": duet_render.LEAD_REL_DB,
        "songs": [{"work_id": t.work_id, "title": t.title, "key": t.key_name, "bpm_counted": round(t.bpm, 2),
                   "regrid": t.factor, "bar_phase_shift": t.meta.get("phase_shift", 0), "lead": t.lead,
                   "lead_stem": v.stem, "vocal_share": round(t.vocal_share, 3), "bars": t.n_bars, "loop_period_bars": plan.periods[j],
                   "shift": v.shift, "key_shift": v.key_shift, "window_start_bar": v.src_bars[0],
                   "window_bars": len(v.src_bars), "distinct_bars": len(set(v.src_bars)),
                   "window_loop_bars": None if v.loop is None else list(v.loop),
                   "window_chord_match": v.chord_match, "window_vocal_cover": v.cover,
                   "tempo_ratio": next(s["tempo_ratio"] for s in entry["songs"] if s["step"] == j),
                   "lead_gain": buses.lead_gains.get(j)}
                  for j, (t, v) in enumerate(zip(plan.tracks, plan.voices))],
        "runs": [{"kind": r.kind, "pair": r.pair, "start": s["start"], "end": s["end"],
                  "instrumental": plan.tracks[r.song].title, "instrumental_bars": list(r.src_bars),
                  "stems": "+".join(r.stems), "vocals": [plan.tracks[x].title for x in r.vocals],
                  "entering": None if r.entering is None else plan.tracks[r.entering].title,
                  "leaving": None if r.leaving is None else plan.tracks[r.leaving].title,
                  "chord_match": r.chord_match, "chord_match_per_bar": list(r.per_bar)}
                 for r, s in zip(plan.runs, entry["segments"])],
    }
    t_verify = time.monotonic()
    if not args.no_verify:
        import soundfile

        dec, dsr = soundfile.read(str(dst), dtype="float32", always_2d=True)
        mono = verify._mono(dec, dsr)
        report["peak"] = verify.peak(dec)
        report["seam"] = duet_verify.seam(tl, dec, dsr, len(normalized))
        report["clicks"] = verify.clicks(normalized, render.SR, [j for j in buses.joins if 0.5 < j < tl.seconds - 0.5],
                                         tl.grid, tl.bpb)
        report["key_check"] = duet_verify.loop_key(tl, mono)
        report["beats"] = duet_verify.loop_beats(tl, dec, dsr)
        leads = {j: y * gain for j, y in buses.leads.items()}
        report["voices"] = {}
        native = {}
        for j, t in enumerate(plan.tracks):
            stem = plan.voices[j].stem
            sl = cache.get(j, stem)
            sb = cache.get(j, "bass") + cache.get(j, {"vocals": "other", "other-high": "other-low"}.get(stem, "bass"))
            report["voices"][t.work_id] = duet_verify.voice_beat_error(tl, j, leads[j], render.SR, sl, render.SR)
            native[t.work_id] = duet_verify.native_share(t, sl, sb, render.SR, list(plan.voices[j].src_bars))
        lead_bus = buses.lead * gain
        report["chords"] = duet_verify.chords(tl, entry["chords"], buses.bed * gain, lead_bus, render.SR, native)
        report["vocal_presence"] = duet_verify.vocal_presence(tl, leads, render.SR)
        report["levels"] = duet_verify.levels(tl, buses.bed * gain, lead_bus, render.SR)
    report["verify_seconds"] = round(time.monotonic() - t_verify, 1)
    return entry, report


def _summary(entry: dict, rep: dict) -> None:
    names = {s["work_id"]: s["title"] for s in entry["songs"]}
    for s in entry["segments"]:
        extra = (f" + {names[s['entering']]} in, {names[s['leaving']]} out" if s["kind"] == "handoff" else "")
        _log(f"  {s['start']:7.2f}-{s['end']:7.2f} {s['kind']:7s} {names[s['instrumental']][:24]:24s}"
             f" | {names[s['vocals'][0]][:20]} & {names[s['vocals'][1]][:20]}{extra}, chord match {s['chord_match']:.2f}")
    for so in rep["songs"]:
        _log(f"  voice {so['title'][:28]:28s} shift {so['shift']:+d} (key {so['key_shift']:+d}), x{so['tempo_ratio']:.3f},"
             f" bars {so['window_start_bar']}+{so['window_bars']} ({so['distinct_bars']} distinct),"
             f" window match {so['window_chord_match']:.2f}, lead {so['lead']}")
    if "seam" in rep:
        k, b, sm, vp = rep["key_check"], rep["beats"], rep["seam"], rep["vocal_presence"]
        errs = [v.get("beat_error_ms") for v in rep["voices"].values() if v.get("beat_error_ms") is not None]
        _log(f"  key heard {k['heard']} vs {k['declared']} ({k['relation']}); beats {b['median_ms']} ms median,"
             f" {b['within_50ms']} within 50 ms, heard {b['bpm_heard']} BPM; worst voice beat error"
             f" {max(errs) if errs else None} ms")
        _log(f"  seam: length delta {sm['length_delta']}, click ratio {sm['click_ratio']} ({'CLICK' if sm['click'] else 'ok'}),"
             f" wrap step ratio {sm['wrap_step_ratio']}; joins clicks {rep['clicks']['clicks']}")
        _log(f"  two vocals heard in {vp['share_two_or_more']:.0%} of seconds {vp['heard_histogram']};"
             f" under two at {vp['seconds_under_two'][:12]}")
    _log(f"  {entry['seconds']:.1f}s loop, {rep['loudness']['lufs']} LUFS, TP {rep['loudness']['true_peak']} dBTP,"
         f" render {rep['render_seconds']}s, verify {rep['verify_seconds']}s")


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
    if args.handoff_bars < 1:
        _log("error: --handoff-bars must be at least 1")
        return 2
    try:
        ffmpeg = find_ffmpeg()
    except FfmpegNotFound as exc:
        _log(f"error: {exc}")
        return 2
    out_dir = duets_dir()
    doc_path = out_dir / "duets.json"
    report_path = out_dir / "duets_report.json"

    def _old(path: Path, key: str) -> dict:
        if not path.is_file():
            return {}
        try:
            return {x["id"]: x for x in json.loads(path.read_text(encoding="utf-8")).get(key, [])}
        except ValueError:
            return {}

    entries, reports, failed = _old(doc_path, "paths"), _old(report_path, "paths"), {}
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
        _summary(entry, rep)
    order = [p["id"] for p in load_paths()]
    doc = {"version": duet_contract.VERSION, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "frame": FRAME, "paths": [entries[i] for i in order if i in entries]}
    errors = duet_contract.validate(doc, out_dir)
    if errors:
        for e in errors[:40]:
            _log(f"contract error: {e}")
        return 1
    _write_json(doc_path, doc)
    _write_json(report_path, {"generated_at": doc["generated_at"],
                              "paths": [reports[i] for i in order if i in reports], "failed": failed},
                indent=1)
    _log(f"\nwrote {doc_path} ({len(doc['paths'])} paths) and {report_path} in {time.monotonic() - t0:.0f}s")
    return 1 if failed else 0

