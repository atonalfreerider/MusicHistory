"""``python -m musichistory mosaic [--examples N] [--target WORK_ID ...] [--workers N] [--research]
[--no-verify] [--search-only]``: melody mosaics (DESIGN §17) - one song's melody rebuilt piece by
piece from other songs' melodies, then harmonized by other melodies, as a ~90 s mix per example.

1. **Notes** (``notes.py``, cached in ``data/audio/mosaics/notes/``) of every preview's vocal
   stem; stems and the per-song analysis are the ``mashup`` stage's (``--all``).
2. **Search** (``search.py``, cached in ``data/audio/mosaics/search.json`` while the parameters
   and notes are unchanged; ``--research`` redoes it): every viable target's loop and its best
   cover by other songs' pieces (``match.py``, ``assemble.py``), in a process pool; harmonies
   (``harmony.py``) for the ``--harmony-top`` best covers.
3. **Rank** targets by mosaic quality (match, recognizable pieces: seconds and notes per piece,
   few pieces; coverage, distinct songs) and harmony quality; eligible targets are rendered in
   that order (at most one per artist) until ``--examples`` have passed the heard-match gate
   (``--min-heard``: the rendered mosaic, transcribed again, must keep that share of its planned
   note-for-note match). ``data/reports/mosaic_report.json``
   lists the top targets with their numbers, the examples and why other candidates were
   passed over.
4. **Render** (``render.py``) as many whole loops as fit in ``--max-seconds``: original ->
   mosaic -> harmony, loudness-normalized MP3 ``data/audio/mosaics/<id>/mix.mp3``.
5. **Verify** (``verify.py``, unless ``--no-verify``): the rendered mosaic re-transcribed against
   the target melody (heard match), harmony consonance heard, clicks at every join, loudness,
   beats; **export** ``data/audio/mosaics/mosaics.json`` (``contract.py``, validated first).

No lyrics anywhere: vocals are audio only, measured as pitch and energy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

import numpy as np

from .. import config
from ..mashup import analysis
from ..mashup import render as mrender
from ..mashup import stems as stems_mod
from ..mashup import verify as mverify
from ..mashup.contract import FRAME
from ..mashup.stage import _write_json
from ..paths.ffmpeg import FfmpegNotFound, find_ffmpeg
from . import assemble, contract, export, harmony, match, notes, render, search, verify

REPORT_TOP = 30
MIN_HEARD = 0.6              # a rendered mosaic whose re-transcribed melody keeps less of its planned match is replaced


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--examples", type=int, default=8, help="mosaics to render (default 8)")
    p.add_argument("--target", action="append", help="work id to render as an example (repeatable)")
    p.add_argument("--workers", type=int, default=4, help="search processes (default 4)")
    p.add_argument("--harmony-top", type=int, default=40, help="best covers searched for harmonies (default 40)")
    p.add_argument("--max-seconds", type=float, default=render.MAX_SECONDS, help="mix length limit (default 90)")
    p.add_argument("--research", action="store_true", help="search again even when the cached search is current")
    p.add_argument("--search-only", action="store_true", help="search and report, render nothing")
    p.add_argument("--min-heard", type=float, default=MIN_HEARD,
                   help=f"share of its planned match a rendered mosaic must keep when heard (default {MIN_HEARD})")
    p.add_argument("--no-verify", action="store_true", help="skip the measurements (and the heard-match gate)")


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def mosaics_dir() -> Path:
    return config.DATA / "audio" / "mosaics"


def report_path() -> Path:
    return config.REPORTS / "mosaic_report.json"


def params() -> dict:
    """Every parameter the cached search depends on."""
    return {"notes": notes.VERSION, "onset_tol": match.ONSET_TOL, "dur_ratio": list(match.DUR_RATIO),
            "dur_abs": match.DUR_ABS, "folds": list(match.FOLDS), "fold_tol": match.FOLD_TOL,
            "tempo_range": list(match.TEMPO_RANGE), "ngram": match.NGRAM, "ratio_class": match.RATIO_CLASS,
            "min_piece": match.MIN_PIECE, "long_rate": match.LONG_RATE, "ornament": match.ORNAMENT,
            "span_bars": [assemble.MIN_SPAN_BARS, assemble.LONG_SPAN_BARS], "max_octave": match.MAX_OCTAVE, "min_rate": match.MIN_RATE, "length_bonus": assemble.LENGTH_BONUS,
            "miss_cost": assemble.MISS_COST, "piece_cost": assemble.PIECE_COST, "dup_cost": assemble.DUP_COST,
            "keep_per_span": assemble.KEEP_PER_SPAN, "loop_bars": list(assemble.LOOP_BARS),
            "ideal_seconds": assemble.IDEAL_SECONDS, "loop_seconds": [assemble.MIN_LOOP_SECONDS, assemble.MAX_LOOP_SECONDS],
            "min_stability": assemble.MIN_STABILITY, "min_loop_notes": assemble.MIN_LOOP_NOTES,
            "clear_vocal_db": search.CLEAR_VOCAL_DB, "octave_bpm": [search.SLOW_BPM, search.FAST_BPM], "melodic": [search.MIN_CHANGES, search.CHANGE_SHARE, search.MAX_REPEAT_SHARE,
                                              search.MIN_LOOP_PITCHES, search.MIN_LOOP_RANGE,
                                              search.MAX_PITCH_DEVIATION, search.MIN_SUNG_STABILITY],
            "harmony": {"cell": harmony.CELL, "consonance": harmony.CONSONANCE.tolist(), "min_coverage": harmony.MIN_COVERAGE,
                        "weights": [harmony.STRONG_W, harmony.DOUBLE_W, harmony.DOUBLE_FREE, harmony.CHORD_W,
                                    harmony.SPREAD_W, harmony.PAIR_W, harmony.PAIR_DOUBLE_W]}}


def params_hash() -> str:
    return hashlib.sha256(json.dumps(params(), sort_keys=True).encode()).hexdigest()[:16]


def notes_fingerprint(corpus: dict) -> str:
    h = hashlib.sha256()
    for w in sorted(corpus):
        h.update(f"{w}:{len(corpus[w].notes.notes)}:{corpus[w].notes.factor}:{corpus[w].source_ok};".encode())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- search
def run_search(corpus: dict, args: argparse.Namespace) -> tuple[list[dict], dict[str, dict]]:
    path = mosaics_dir() / "search.json"
    key = {"params": params_hash(), "corpus": notes_fingerprint(corpus)}
    cached = {}
    if path.is_file() and not args.research:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            cached = {}
    if cached.get("key") == key:
        results = cached["results"]
        harm = cached.get("harmonies") or {}
        _log(f"search: cached ({len(results)} songs)")
    else:
        t0 = time.monotonic()
        _log(f"search: {len(corpus)} songs with {args.workers} workers ...")
        results = search.run_pool(search.evaluate_target, [(w,) for w in sorted(corpus)], args.workers, _log, "targets")
        results.sort(key=lambda r: r["work_id"])
        harm = {}
        _log(f"search: done in {time.monotonic() - t0:.0f}s")
    ranked = sorted((r for r in results if not search.eligible(r)), key=lambda r: -r["quality"])
    want = [r for r in ranked[:args.harmony_top]]
    for w in args.target or []:
        r = next((x for x in results if x["work_id"] == w), None)
        if r is not None and "loop" in r and r not in want:
            want.append(r)
    todo = [(r["work_id"], r) for r in want if r["work_id"] not in harm]
    if todo:
        t0 = time.monotonic()
        _log(f"harmonies: {len(todo)} targets ...")
        for h in search.run_pool(search.harmonize, todo, args.workers, _log, "harmonies"):
            harm[h["work_id"]] = h
        _log(f"harmonies: done in {time.monotonic() - t0:.0f}s")
    _write_json(path, {"key": key, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "results": results, "harmonies": harm})
    return results, harm


def candidates(results: list[dict], harm: dict, forced: list[str]) -> tuple[list[dict], list[dict]]:
    """(eligible targets by total score - forced ones first -, ineligible ones with the reason)."""
    scored = []
    for r in results:
        if "loop" not in r:
            continue
        h = harm.get(r["work_id"])
        scored.append({**r, "harmony": h, "total": search.total(r, h)})
    scored.sort(key=lambda r: -r["total"])
    ok, passed = [], []
    for r in [x for x in scored if x["work_id"] in forced] + [x for x in scored if x["work_id"] not in forced]:
        why = search.eligible(r)
        if not why and not (r["harmony"] and r["harmony"]["voices"]):
            why = "no harmony voice found" if r["harmony"] else "not among the best covers searched for harmonies"
        (passed if why else ok).append({**r, "why": why} if why else r)
    return ok, passed


# --------------------------------------------------------------------------- one example
def meta(s) -> dict:
    return {"work_id": s.work_id, "title": s.title, "artist": s.artist, "year": int(s.year)}


def build_example(r: dict, corpus: dict, ffmpeg: str, args: argparse.Namespace) -> tuple[dict, dict]:
    t_start = time.monotonic()
    wid = r["work_id"]
    s = corpus[wid]
    tr, loop = search.loop_of(s, r)
    lb = loop.beats
    an = json.loads(analysis.analysis_path(wid).read_text(encoding="utf-8"))
    pieces = sorted((search.piece_from(d) for d in r["pieces"]), key=lambda p: p.a)
    voices = r["harmony"]["voices"]
    pspecs = []
    for p in pieces:
        src = corpus[p.work_id]
        pspecs.append(render.PieceSpec(p.work_id, src.notes.beats, src.notes.tuning, p.fold, p.offset, p.shift,
                                       float(p.fold * src.seq.on[p.sa] + p.offset),
                                       float(p.fold * src.seq.off[p.sb] + p.offset), src.notes.duration))
    vspecs = [render.VoiceSpec(v["work_id"], corpus[v["work_id"]].notes.beats, corpus[v["work_id"]].notes.tuning,
                               v["fold"], v["start"], v["shift"], corpus[v["work_id"]].notes.duration) for v in voices]
    spec = render.Spec(wid, s.notes.beats, loop.beat0, lb, loop.bpb, s.notes.tuning, an.get("levels_db") or {},
                       pspecs, vspecs, args.max_seconds)
    ids = {wid} | {p.work_id for p in pieces} | {v["work_id"] for v in voices}
    stems = mrender.StemCache({w: stems_mod.stems_root() / w for w in ids})
    rendered = render.render(spec, stems, log=_log)
    n_songs = len({p.work_id for p in pieces})
    mosaic_id, name, title = export.names(s.title, n_songs)
    dst = mosaics_dir() / contract.mix_file(mosaic_id)
    loud = mrender.write_mp3(ffmpeg, rendered.mix, dst)
    normalized = loud.pop("normalized")
    render_s = time.monotonic() - t_start

    f0 = export.frame_offset(s.tonic, s.mode)
    piece_objs, piece_rep = [], []
    for p, ps, cut in zip(pieces, pspecs, rendered.cuts):
        src = corpus[p.work_id]
        tmap = render.map_points(ps.src_beats, rendered.grid, p.fold, p.offset, ps.first, ps.last, ps.duration)
        ratio = float(np.median(np.diff(tmap.src) / np.diff(tmap.dst))) if tmap is not None else p.tempo_ratio
        seq = src.seq
        # where the piece is heard (lead-in to hand-over), in loop beats and source seconds
        xa, xb = (float(x) for x in rendered.grid.beat(np.array(cut)))
        ua, ub = (xa - p.offset) / p.fold, (xb - p.offset) / p.fold
        on, off = p.fold * seq.on + p.offset, p.fold * seq.off + p.offset
        sel = (on < xb) & (off > xa)
        heard = export.note_rows(np.maximum(on[sel], xa), np.minimum(off[sel], xb), seq.pitch[sel] + p.shift, lb, 0)
        start, end = round(float(np.clip(xa, 0, lb)), 4), round(float(np.clip(xb, 0, lb)), 4)
        s0 = max(float(notes.to_seconds(ua, src.notes.beats)), 0.0)
        s1 = min(float(notes.to_seconds(ub, src.notes.beats)), src.notes.duration or 1e9)
        piece_objs.append({**meta(src), "start": start, "end": end, "source_start": round(s0, 3),
                           "source_end": round(s1, 3), "shift_semitones": int(p.shift), "tempo_ratio": round(ratio, 4),
                           "match": round(p.rate, 4), "notes": heard})
        span = (round(float(loop.notes.on[p.a]), 3), round(float(min(match.held_until(loop.notes)[p.b], lb)), 3))
        piece_rep.append({"title": src.title, "artist": src.artist, "year": src.year, "work_id": p.work_id,
                          "loop_beats": [start, end], "heard_seconds": round(cut[1] - cut[0], 2),
                          "matched_beats": list(span), "target_notes": [p.a, p.b],
                          "source_seconds": [round(s0, 2), round(s1, 2)], "shift": p.shift, "octave": p.octave,
                          "fold": p.fold, "tempo_ratio": round(ratio, 4), "matched": p.matched,
                          "of": max(p.n_target, p.n_source), "match": round(p.rate, 4)})
    harm_objs, harm_rep = [], []
    for v in voices:
        src = corpus[v["work_id"]]
        vo = harmony.Voice(v["work_id"], v["fold"], v["start"], v["shift"], v["tempo_ratio"], v["score"],
                           v["consonance"], v["coverage"], v["strong"], v["doubling"], v["chord_fit"], v["spread"])
        hn = [[round(a, 4), round(b, 4), p] for a, b, p in harmony.heard_notes(src.seq, vo, lb)]
        harm_objs.append({**meta(src), "shift_semitones": int(v["shift"]), "tempo_ratio": round(v["tempo_ratio"], 4),
                          "consonance": round(v["consonance"], 4), "notes": hn})
        harm_rep.append({"title": src.title, "artist": src.artist, "year": src.year, "work_id": v["work_id"],
                         "shift": v["shift"], "fold": v["fold"], "source_start_beat": v["start"],
                         "tempo_ratio": v["tempo_ratio"], **{k: v[k] for k in ("score", "consonance", "coverage", "strong",
                                                                                  "doubling", "chord_fit", "spread")}})
    mo = assemble.Mosaic(pieces, len(loop.notes))
    entry = export.mosaic_entry(mosaic_id=mosaic_id, name=name, title=title, target=meta(s), key=s.key, f0=f0, r=rendered,
                                bpb=loop.bpb, slot_chords=search.slot_chords(tr, loop), mode=s.mode,
                                target_notes=loop.notes, pieces=piece_objs, harmonies=harm_objs,
                                coverage=mo.coverage, match=mo.match)
    rep: dict = {
        "id": mosaic_id, "name": name, "title": title, "target": meta(s), "key": s.key, "bpm": entry["bpm"],
        "beats_per_bar": loop.bpb, "loop": {"start_bar": loop.start_bar, "bars": loop.bars,
                                            "seconds": round(rendered.grid.seconds, 3), "notes": len(loop.notes),
                                            "chord_seam": loop.seam},
        "sections": entry["sections"], "seconds": entry["seconds"], "loops": rendered.n_loops,
        "coverage": round(mo.coverage, 4), "match": round(mo.match, 4), "pieces": piece_rep,
        "mean_piece_notes": round(mo.mean_notes, 2), "longest_piece": max(p.matched for p in pieces),
        "mean_piece_heard_seconds": round(float(np.mean([c[1] - c[0] for c in rendered.cuts])), 2),
        "harmonies": harm_rep, "quality": r["quality"], "harmony_quality": round(search.harmony_quality(r["harmony"]), 4),
        "total": r["total"], "gains": rendered.gains, "loudness": loud, "render_seconds": round(render_s, 1)}
    t_verify = time.monotonic()
    if not args.no_verify:
        import soundfile

        dec, dsr = soundfile.read(str(dst), dtype="float32", always_2d=True)
        spans = [(float(loop.notes.on[p.a]), float(loop.notes.off[p.b])) for p in pieces]
        rep["heard_match"] = verify.heard_match(rendered, loop.notes, spans, s.notes.tuning, render.SR)
        for pr, h in zip(rep["pieces"], rep["heard_match"]["pieces"]):
            pr["heard"] = h
        for hv, hh in zip(rep["harmonies"], verify.harmony_heard(rendered, loop.notes, s.notes.tuning, render.SR)):
            hv.update(hh)
        rep["clicks"] = verify.clicks(rendered, normalized, loop.bpb, render.SR)
        rep["peak"] = mverify.peak(dec)
        rep["beats"] = verify.beats(rendered, dec, dsr)
        rep["joins"] = len(rendered.joins)
    rep["verify_seconds"] = round(time.monotonic() - t_verify, 1)
    return entry, rep


def _summary(rep: dict) -> None:
    t = rep["target"]
    lp = rep["loop"]
    _log(f"  {rep['name']!r}: {t['title']} ({t['artist']}, {t['year']}) {rep['key']}, {rep['bpm']} BPM,"
         f" loop {lp['bars']} bars = {lp['seconds']}s / {lp['notes']} notes, {rep['loops']} loops = {rep['seconds']}s")
    _log("  sections: " + ", ".join(f"{s['kind']} x{s['loops']}" for s in rep["sections"]))
    for p in rep["pieces"]:
        oct_ = f" oct {p['octave']}" if p["octave"] else ""
        heard = f", heard {p['heard']:.2f}" if "heard" in p else ""
        _log(f"    piece beats {p['loop_beats'][0]:5.2f}-{p['loop_beats'][1]:5.2f} ({p['heard_seconds']:.1f}s)"
             f" {p['title'][:30]:30s} ({p['year']})"
             f" src {p['source_seconds'][0]:5.2f}-{p['source_seconds'][1]:5.2f}s shift {p['shift']:+d}{oct_}"
             f" x{p['tempo_ratio']:.2f} match {p['matched']}/{p['of']}{heard}")
    _log(f"  coverage {rep['coverage']:.2f}, match {rep['match']:.2f}, mean piece {rep['mean_piece_notes']} notes"
         f" / {rep['mean_piece_heard_seconds']} s heard,"
         f" longest {rep['longest_piece']}")
    for h in rep["harmonies"]:
        heard = h.get("heard_consonance")
        _log(f"    harmony {h['title'][:30]:30s} shift {h['shift']:+d} x{h['tempo_ratio']:.2f} consonance"
             f" {h['consonance']:.2f}{'' if heard is None else f' (heard {heard:.2f})'} coverage {h['coverage']:.2f}"
             f" strong-beat dissonance {h['strong']:.2f} doubling {h['doubling']:.2f}")
    if "heard_match" in rep:
        hm, c = rep["heard_match"], rep["clicks"]
        _log(f"  heard match {hm['octave']:.2f} (exact {hm['exact']:.2f}, inside pieces {hm['in_pieces']:.2f};"
             f" the target's own vocal {hm['baseline']}); clicks {c['clicks']} worst {c['worst_ratio']} over"
             f" {c['checked']} joins; beats {rep['beats']['median_ms']} ms median")
    _log(f"  {rep['loudness']['lufs']} LUFS, TP {rep['loudness']['true_peak']} dBTP, render {rep['render_seconds']}s,"
         f" verify {rep['verify_seconds']}s")


def target_row(r: dict, corpus: dict) -> dict:
    s = corpus[r["work_id"]]
    row = {"work_id": r["work_id"], "title": s.title, "artist": s.artist, "year": s.year, "key": s.key,
           "bpm": round(s.notes.bpm, 1), "total": r.get("total"), "quality": r.get("quality")}
    if "loop" in r:
        row.update({"loop": r["loop"], "match": r["match"], "coverage": r["coverage"], "pieces": len(r["pieces"]),
                    "songs": r["songs"], "mean_piece_notes": r["mean_notes"], "longest_piece": r["longest"],
                    "mean_piece_seconds": r.get("mean_seconds"),
                    "mean_piece_match": r["mean_rate"], "candidates": r["candidates"],
                    "piece_songs": [f"{corpus[p['work_id']].title} ({p['matched']}/{max(p['n_target'], p['n_source'])})"
                                    for p in r["pieces"]]})
    h = r.get("harmony")
    if h:
        row["harmonies"] = [{"title": corpus[v["work_id"]].title, "artist": corpus[v["work_id"]].artist,
                             "score": v["score"], "consonance": v["consonance"], "coverage": v["coverage"],
                             "strong": v["strong"], "doubling": v["doubling"]} for v in h["voices"]]
        row["harmony_quality"] = round(search.harmony_quality(h), 4)
    if "why" in r:
        row["why_not"] = r["why"]
    return row


# --------------------------------------------------------------------------- run
def run(args: argparse.Namespace) -> int:
    t0 = time.monotonic()
    _log("corpus: loading notes ...")
    corpus = search.load_corpus(log=_log, workers=args.workers)
    sources = sum(1 for s in corpus.values() if s.source_ok)
    _log(f"corpus: {len(corpus)} songs, {sources} usable as sources")
    results, harm = run_search(corpus, args)
    forced = list(args.target or [])
    unknown = [w for w in forced if w not in corpus]
    if unknown:
        _log(f"error: unknown target(s): {', '.join(unknown)}")
        return 2
    ok, passed = candidates(results, harm, forced)
    reasons: dict[str, int] = {}
    for r in results:
        why = search.eligible(r)
        if why:
            key = why.split(" (")[0]
            reasons[key] = reasons.get(key, 0) + 1
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "params": params(),
              "corpus": {"songs": len(corpus), "sources": sources, "targets_searched": len(results),
                         "targets_with_cover": sum(1 for r in results if "loop" in r), "not_eligible": reasons},
              "top": [], "chosen": [], "passed_over": [], "examples": [], "failed": {}}
    gate = None if args.no_verify else args.min_heard
    entries: list[dict] = []
    artists: set[str] = set()
    ffmpeg = None
    if not args.search_only:
        try:
            ffmpeg = find_ffmpeg()
        except FfmpegNotFound as exc:
            _log(f"error: {exc}")
            return 2
    for r in ok:
        s = corpus[r["work_id"]]
        is_forced = r["work_id"] in forced
        if len(report["chosen"]) >= args.examples and not is_forced:
            passed.append({**r, "why": "ranked below the chosen examples"})
            continue
        if s.artist in artists and not is_forced:
            passed.append({**r, "why": f"artist {s.artist} already has an example"})
            continue
        if args.search_only:
            report["chosen"].append(r["work_id"])
            artists.add(s.artist)
            continue
        _log()
        _log(f"{r['work_id']}: {s.title}")
        try:
            entry, rep = build_example(r, corpus, ffmpeg, args)
        except Exception as exc:  # one example must not stop the others
            import traceback

            report["failed"][r["work_id"]] = f"{type(exc).__name__}: {exc}"
            _log(f"  FAILED: {report['failed'][r['work_id']]}")
            _log(traceback.format_exc())
            continue
        _summary(rep)
        heard = (rep.get("heard_match") or {}).get("octave")
        if gate is not None and heard is not None and heard < gate * rep["match"] and not is_forced:
            shutil.rmtree(mosaics_dir() / entry["id"], ignore_errors=True)
            why = (f"heard match {heard:.2f} under {gate:.0%} of the planned {rep['match']:.2f} after rendering")
            _log(f"  rejected: {why}")
            passed.append({**r, "why": why, "heard": rep["heard_match"]})
            continue
        entries.append(entry)
        report["examples"].append(rep)
        report["chosen"].append(r["work_id"])
        artists.add(s.artist)
    ranked = sorted([r for r in ok if r["work_id"] in report["chosen"]] + [p for p in passed if "loop" in p],
                    key=lambda r: -(r.get("total") or 0))
    report["top"] = [target_row(r, corpus) for r in ranked[:REPORT_TOP]]
    report["passed_over"] = [{"work_id": p["work_id"], "title": corpus[p["work_id"]].title, "total": p.get("total"),
                              "why": p["why"]} for p in sorted(passed, key=lambda r: -(r.get("total") or 0))
                             if "loop" in p][:REPORT_TOP]
    _log()
    _log(f"chosen: {', '.join(corpus[w].title for w in report['chosen'])}")
    if args.search_only:
        _write_json(report_path(), report, indent=1)
        _log(f"wrote {report_path()} in {time.monotonic() - t0:.0f}s")
        return 0
    out_dir = mosaics_dir()
    doc = {"version": contract.VERSION, "generated_at": report["generated_at"], "frame": FRAME, "mosaics": entries}
    errors = contract.validate(doc, out_dir)
    if errors:
        for e in errors[:40]:
            _log(f"contract error: {e}")
        _write_json(report_path(), report, indent=1)
        return 1
    _write_json(out_dir / "mosaics.json", doc)
    _write_json(report_path(), report, indent=1)
    keep = {e["id"] for e in entries}
    for d in out_dir.iterdir():                       # mixes of earlier runs no longer listed
        if d.is_dir() and d.name not in keep and (d / "mix.mp3").is_file():
            shutil.rmtree(d, ignore_errors=True)
    _log(f"\nwrote {out_dir / 'mosaics.json'} ({len(entries)} mosaics) and {report_path()} in {time.monotonic() - t0:.0f}s")
    return 1 if report["failed"] else 0
