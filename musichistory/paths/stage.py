"""``python -m musichistory paths [--pick-only] [--force]``: featured paths through the
identity-lineage graph with prerendered recording previews (contract: ``contract.py``).

1. **Measure** every preview (``data/audio/<work_id>/preview.mp3``): tempo, key and a per-beat
   chord/bass reading (``audio.py``; cached in ``data/audio/analysis.json``, so reruns are
   instant; ``--remeasure`` drops the cache). The MIDI analysis is a prior only; agreement
   with it is reported.
2. **Curate**: the paths in ``musichistory/paths/curated.json`` (hand-editable; each is checked
   against the rules in ``curate.py``), or with ``--auto`` (or an empty file) the best diverse
   automatic picks. The automatic ranking and the old ``index.json`` paths are evaluated
   and listed either way.
3. **Render** every step (``render.py``): 44.1 kHz MP3, loudness-normalized, starting in the
   previous step's key and tempo as heard and gliding to its own over ``morph_bars`` bars
   (smoothstep), then native; 30 ms fade-in, 1.5 s fade-out. A step whose file and plan are
   unchanged is kept (``--force`` re-renders). Writes ``data/audio/renders/paths.json`` and a
   report ``data/audio/renders/paths_report.json``. The old ``index.json`` and its files are
   left in place.

``--pick-only`` stops after step 2. Audio stays under ``data/`` (gitignored).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

from .. import config
from . import audio, contract, curate, graph as graph_mod, measured as measured_mod, render
from .ffmpeg import FfmpegNotFound, find_ffmpeg, has_filter
from .plan import MORPH_BARS, plan_step


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pick-only", action="store_true", help="measure, curate and report; render nothing")
    p.add_argument("--force", action="store_true", help="re-render every step even when its file is up to date")
    p.add_argument("--remeasure", action="store_true", help="measure every preview again (ignore analysis.json)")
    p.add_argument("--auto", action="store_true", help="ignore curated.json and use the automatic picks")
    p.add_argument("--count", type=int, default=12, help="automatic picks with --auto (default 12)")
    p.add_argument("--list", type=int, default=30, help="automatic candidates to print (default 30)")
    p.add_argument("--curated", help="curated paths file (default musichistory/paths/curated.json)")
    p.add_argument("--workers", type=int, help="processes for measuring previews (default min(4, CPUs))")
    p.add_argument("--morph-bars", type=float, default=MORPH_BARS, help="glide length in bars (default 2)")


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def audio_dir() -> Path:
    return config.DATA / "audio"


# --------------------------------------------------------------------------- describe
def hop_line(g: graph_mod.Graph, h: curate.Hop) -> str:
    e = h.edge
    flag = f"strong z {e.z:.1f}" if e.strong else ("generic" if h.generic else "")
    checks = []
    for name, c in (("src", h.src_check), ("dst", h.dst_check)):
        if c is not None and c.checked:
            checks.append(f"{name} {'heard' if c.in_key else ('heard@other key' if c.any_key else 'not heard')}")
    if h.pair is not None:
        if h.pair.kind == "chord_run":
            checks.append(f"shared chord run {int(h.pair.value)}")
        else:
            checks.append(f"chroma sim pct {h.pair.value:.2f}")
    smooth = "" if h.smooth else "  ROUGH"
    return (f"      via [{e.kind}] {e.evidence} (w {h.weight:.2f}{', ' + flag if flag else ''}; family "
            f"{e.family.size if e.family else '?'}) | {h.semitones:+d} st, x{h.ratio:.2f} tempo{smooth}"
            + (f" | {'; '.join(checks)}" if checks else ""))


def describe(g: graph_mod.Graph, m: dict[int, measured_mod.Measured], c: curate.Candidate, head: str) -> list[str]:
    t = c.terms
    lines = [f"{head}  score {c.score:.2f}  (identity {t['identity']:.2f}, fame {t['fame']:.2f}, span {t['span']:.2f},"
             f" coherence {t['coherence']:.2f}, audible {t['audible']:+.2f}, smooth cost {t['smooth_cost']:.2f},"
             f" rough {t['rough_hops']}, generic {t['generic_hops']})"]
    for i, nid in enumerate(c.nodes):
        s, x = g.songs[nid], m[nid]
        if i:
            lines.append(hop_line(g, c.hops[i - 1]))
        lines.append(f"   {s.year}  {s.title} - {s.artist}  [{x.key_name} {x.bpm:.1f} BPM;"
                     f" MIDI {s.key_name} {s.native_bpm:.0f}]")
    return lines


def hop_record(h: curate.Hop) -> dict:
    def clip(c):
        return None if c is None else {"checked": c.checked, "in_key": c.in_key, "any_key": c.any_key, "note": c.note}
    return {"identity": h.edge.evidence, "edge_kind": h.edge.kind, "strong": h.edge.strong, "z": round(h.edge.z, 2),
            "weight": h.weight, "generic": h.generic, "start_semitones": h.semitones,
            "start_bpm": round(h.start_bpm, 2), "bpm": round(h.bpm, 2), "tempo_ratio": round(h.ratio, 3),
            "smooth": h.smooth, "source_clip": clip(h.src_check), "target_clip": clip(h.dst_check),
            "pair": None if h.pair is None else {"kind": h.pair.kind, "passed": h.pair.passed,
                                                  "value": h.pair.value, **h.pair.detail}}


def candidate_record(g: graph_mod.Graph, c: curate.Candidate) -> dict:
    return {"works": [g.songs[n].work_id for n in c.nodes],
            "songs": [f"{g.songs[n].year} {g.songs[n].title} - {g.songs[n].artist}" for n in c.nodes],
            "score": c.score, "terms": c.terms, "hops": [hop_record(h) for h in c.hops]}


# --------------------------------------------------------------------------- legacy
def evaluate_legacy(g: graph_mod.Graph, m: dict[int, measured_mod.Measured], checker: curate.Checker,
                    renders: Path) -> list[dict]:
    idx = renders / "index.json"
    if not idx.exists():
        return []
    try:
        doc = json.loads(idx.read_text(encoding="utf-8"))
    except ValueError:
        return []
    out = []
    for p in doc.get("paths") or []:
        works = [g.songs[n].work_id for n in p.get("nodes") or [] if n in g.songs]
        cp = curate.CuratedPath("legacy", p.get("select_title", "?"), "", works)
        errors = curate.validate_curated(g, m, cp)
        rec = {"select_title": p.get("select_title"), "works": works, "rule_errors": errors}
        if not [e for e in errors if "not measured" in e or "no edge" in e or "not in the graph" in e]:
            c = curate.curated_candidate(g, m, cp, checker)
            rec.update(candidate_record(g, c))
            rough = c.terms["rough_hops"]
            generic = c.terms["generic_hops"]
            weak = [h.edge.evidence for h in c.hops if h.weight < 0.5]
            reasons = []
            if generic:
                reasons.append(f"{generic} generic hop(s)")
            if rough:
                reasons.append(f"{rough} rough transition(s)")
            if weak:
                reasons.append("weak identities: " + ", ".join(dict.fromkeys(weak)))
            rec["verdict"] = "keep" if not reasons and not errors else "drop"
            rec["reasons"] = reasons + errors
        else:
            rec["verdict"] = "drop"
            rec["reasons"] = errors
        out.append(rec)
    return out


# --------------------------------------------------------------------------- run
def run(args: argparse.Namespace) -> int:
    t_start = time.monotonic()
    adir = audio_dir()
    renders = adir / "renders"
    g = graph_mod.load(config.GRAPH_DB, adir)
    previews = {s.work_id: s.preview for s in g.songs.values() if s.preview is not None}
    _log(f"graph: {len(g.songs)} songs, {len(g.edges)} edges; previews for {len(previews)}")

    # 1. measure
    analysis, stats = audio.analyze_all(previews, adir / "analysis.json", force=args.remeasure,
                                        workers=args.workers, log=_log)
    _log(f"measure: {stats['cached']} cached, {stats['measured']} measured in {stats['seconds']}s,"
         f" {len(stats['failed'])} failed")
    for wid, err in stats["failed"].items():
        _log(f"  failed {wid}: {err}")
    m = measured_mod.resolve_all(g, analysis)
    agree = measured_mod.agreement(m)
    _log("measurement vs MIDI: " + json.dumps({k: agree[k] for k in (
        "key_source", "bpm_source", "audio_key_exact_agreement", "audio_tonic_agreement", "audio_key_relation_to_midi",
        "key_prior_used", "tempo_agreement_4pct", "tempo_ratio_buckets", "tempo_octave_factor")}, ensure_ascii=False))
    _log("tempo percentiles: " + json.dumps(agree["bpm_percentiles"]))

    # 2. curate
    checker = curate.Checker(g, m)
    t0 = time.monotonic()
    cands = curate.enumerate_paths(g, m, checker)
    auto = curate.select(cands, args.count)
    _log(f"\ncurate: {len(cands)} candidate paths in {time.monotonic() - t0:.1f}s; top automatic picks:")
    for i, c in enumerate(curate.select(cands, args.list), 1):
        for line in describe(g, m, c, f"  A{i}"):
            _log(line)
    legacy = evaluate_legacy(g, m, checker, renders)
    if legacy:
        _log("\nold index.json paths:")
        for rec in legacy:
            _log(f"  {rec['select_title']}: {rec['verdict']} ({'; '.join(rec['reasons']) or 'meets the rules'})")

    curated_file = Path(args.curated) if args.curated else curate.CURATED_PATH
    chosen: list[tuple[str, str, str, str | None, curate.Candidate]] = []
    if not args.auto:
        for cp in curate.load_curated(curated_file):
            errors = curate.validate_curated(g, m, cp)
            if errors:
                _log(f"curated path {cp.id} skipped: {'; '.join(errors)}")
                continue
            chosen.append((cp.id, cp.title, cp.description, cp.subtitle, curate.curated_candidate(g, m, cp, checker)))
    if not chosen:
        _log("using the automatic picks" + ("" if args.auto else f" ({curated_file} has no valid path)"))
        for c in auto:
            slug, title, desc = curate.auto_title(g, c)
            chosen.append((slug, title, desc, None, c))
    _log(f"\nfeatured paths ({len(chosen)}):")
    for pid, title, _, _, c in chosen:
        for line in describe(g, m, c, f"  {pid}: {title}"):
            _log(line)

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "measurement": agree,
              "measure_stats": {k: v for k, v in stats.items() if k != "failed"}, "failed": stats["failed"],
              "candidates": len(cands), "automatic": [candidate_record(g, c) for c in curate.select(cands, args.list)],
              "legacy": legacy, "featured": []}
    if args.pick_only:
        _write_json(renders / "paths_report.json", report)
        _log(f"\n--pick-only: nothing rendered; report {renders / 'paths_report.json'}")
        return 0

    # 3. render
    try:
        ffmpeg = find_ffmpeg()
    except FfmpegNotFound as exc:
        _log(f"error: {exc}")
        return 2
    if not has_filter(ffmpeg, "rubberband"):
        _log(f"error: {ffmpeg} has no rubberband filter")
        return 2
    state_path = renders / ".render_state.json"
    state = _read_json(state_path) or {}
    paths_out = []
    t_render = time.monotonic()
    rendered = kept = 0
    verify_counts: Counter = Counter()
    for pid, title, desc, sub, c in chosen:
        steps = []
        prev = None
        rec = candidate_record(g, c)
        rec.update(id=pid, title=title, steps=[])
        for i, nid in enumerate(c.nodes):
            s, x = g.songs[nid], m[nid]
            bpm = round(x.bpm, 2)
            p = plan_step(None if prev is None else prev[1].tonic, None if prev is None else prev[2],
                          x.tonic, bpm, beats_per_bar=s.beats_per_bar, morph_bars=args.morph_bars)
            rel = contract.step_file(pid, i, s.work_id)
            dst = renders / rel
            sig = (f"{render.RENDER_VERSION}|{audio.file_id(s.preview)}|{p.start_semitones}|{p.start_bpm:.3f}|"
                   f"{p.bpm:.3f}|{p.morph_seconds:.4f}|{p.morph}")
            if not args.force and dst.exists() and state.get(rel, {}).get("sig") == sig:
                seconds = state[rel]["seconds"]
                kept += 1
            else:
                seconds = render.render_step(ffmpeg, s.preview, dst, p)
                check = render.verify_start(dst, s.preview, p) if p.morph else {"ok": None}
                state[rel] = {"sig": sig, "seconds": round(seconds, 3), "verify": check}
                rendered += 1
                _log(f"  rendered {rel}: {p.start_semitones:+d} st, {p.start_bpm:.1f} -> {p.bpm:.1f} BPM,"
                     f" glide {p.morph_seconds:.2f}s, {seconds:.1f}s, start check {check}")
                _write_json(state_path, state)
            verify_counts[str(state[rel].get("verify", {}).get("ok"))] += 1
            via = None
            if prev is not None:
                e = g.edge(prev[0], nid)
                via = {"identity": e.evidence, "strong": bool(e.strong), "z": round(e.z, 2), "edge_kind": e.kind,
                       "family_size": int(e.family.size if e.family else 2)}
            steps.append({
                "work_id": s.work_id, "title": s.title, "artist": s.artist, "year": int(s.year),
                "file": rel, "seconds": round(float(seconds), 3), "via": via,
                "start_key": x.key_name if prev is None else prev[1].key_name, "key": x.key_name,
                "start_semitones": int(p.start_semitones), "start_bpm": round(p.start_bpm, 2), "bpm": bpm,
                "morph_seconds": round(p.morph_seconds, 3), "key_source": x.key_source, "bpm_source": x.bpm_source,
            })
            rec["steps"].append({"file": rel, "verify_start": state[rel].get("verify")})
            prev = (nid, x, bpm)
        total = round(sum(st["seconds"] for st in steps), 3)
        paths_out.append({"id": pid, "title": title,
                          "subtitle": sub or curate.subtitle(g, c),
                          "description": desc, "identity": c.main_identity, "seconds": total, "steps": steps})
        report["featured"].append(rec)
    doc = {"version": contract.VERSION, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "morph_bars": args.morph_bars if args.morph_bars != int(args.morph_bars) else int(args.morph_bars),
           "paths": paths_out}
    errors = contract.validate(doc, renders)
    if errors:
        for e in errors:
            _log(f"contract error: {e}")
        return 1
    _write_json(renders / "paths.json", doc, indent=1)
    render_s = time.monotonic() - t_render
    report["render"] = {"rendered": rendered, "kept": kept, "seconds": round(render_s, 1),
                        "start_checks": dict(verify_counts), "ffmpeg": ffmpeg}
    _write_json(renders / "paths_report.json", report)
    stale = sorted(d.name for d in renders.iterdir() if d.is_dir() and d.name not in {p["id"] for p in paths_out}
                   and d.name != "legacy")
    _log(f"\nrender: {rendered} rendered, {kept} unchanged in {render_s:.1f}s; start checks {dict(verify_counts)}")
    if stale:
        _log(f"note: folders not in paths.json (left in place): {', '.join(stale)}")
    _log(f"wrote {renders / 'paths.json'} ({len(paths_out)} paths) in {time.monotonic() - t_start:.1f}s total")
    return 0


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, doc, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
