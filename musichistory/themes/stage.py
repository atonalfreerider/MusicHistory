"""``python -m musichistory themes``: lyric themes of every selected song (DESIGN.md §12).

1. **Read** each selected work's lyrics into memory (``lyrics.py``: Lakh tarballs streamed
   once, web candidates re-downloaded into memory unless ``--no-web``); works without
   usable lyrics are classified from their title.
2. **Classify** (``--backend nli`` default, local GPU; ``--backend claude`` with
   ``ANTHROPIC_API_KEY``) into a ten-way relatability distribution. Resumable: a work is
   skipped when ``song_text`` already holds the same input SHA-256, model and backend
   (``--force`` re-classifies). Rows are committed after every batch. With the Claude
   backend, a refused or failed song falls back to the NLI backend.
3. **Validate** against ``tests/themes/validation_set.json`` (top-1/top-2, by text source,
   plus title-only accuracy with the NLI backend).
4. **Singers**: ``singer.resolve_singers(conn, work_ids)`` (``musichistory/themes/singer.py``,
   separate owner; 'unknown' for every song when it is missing or fails).
5. **Export** ``data/graph/themes_graph.db`` (``export.py``) and a numbers-only report in
   ``data/reports/themes_<timestamp>.json``.

Lyric text is dropped from memory as soon as its song is classified and never printed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

import numpy as np

from .. import config, db
from . import export, lyrics, validation
from .classify import THEMES, Item, NliClassifier, make_backend, top_themes


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", choices=("nli", "claude"), default="nli",
                   help="nli = local zero-shot NLI on the GPU (default); claude = Anthropic API (needs ANTHROPIC_API_KEY)")
    p.add_argument("--no-web", action="store_true",
                   help="do not re-download web candidates (those works use another candidate or their title)")
    p.add_argument("--force", action="store_true", help="re-classify works whose cache key matches")
    p.add_argument("--work", action="append", help="only this work_id (repeatable)")
    p.add_argument("--limit", type=int, help="only the first N selected works by canon_rank")
    p.add_argument("--batch-songs", type=int, default=32, help="songs per classification batch (commit unit)")
    p.add_argument("--batch-size", type=int, default=64, help="NLI premise/hypothesis pairs per GPU batch")
    p.add_argument("--device", help="torch device for the NLI model (default: cuda when available)")
    p.add_argument("--export-only", action="store_true", help="skip reading/classifying; re-export the graph")
    p.add_argument("--no-export", action="store_true", help="do not write themes_graph.db")
    p.add_argument("--no-singer", action="store_true", help="do not call singer.resolve_singers")
    p.add_argument("--tune", action="store_true",
                   help="compare classifier settings on the validation set and print the numbers (no writes)")
    p.add_argument("--out", help="themes graph path (default data/graph/themes_graph.db)")


def _log(msg: str) -> None:
    print(msg, flush=True)


def selected_works(conn: sqlite3.Connection, ids: list[str] | None = None, limit: int | None = None) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT work_id, title, canonical_artist, work_year, canon_rank FROM work WHERE selected >= 1"
                        " ORDER BY canon_rank IS NULL, canon_rank, work_id").fetchall()
    if ids:
        keep = set(ids)
        rows = [r for r in rows if r["work_id"] in keep]
    if limit:
        rows = rows[:limit]
    return rows


# ------------------------------------------------------------------ classification
def classify_works(conn: sqlite3.Connection, works: list[sqlite3.Row], args: argparse.Namespace) -> dict:
    t0 = time.monotonic()
    planned = lyrics.plan(conn, [w["work_id"] for w in works])
    lyr, rstats = lyrics.read(conn, planned, web=not args.no_web, progress=_log)
    read_s = time.monotonic() - t0
    items: list[Item] = []
    source_of: dict[str, tuple[int | None, int]] = {}      # work_id -> (candidate_id, n_lines)
    for w in works:
        ly = lyr.get(w["work_id"])
        items.append(Item(w["work_id"], w["title"], list(ly.lines) if ly else None))
        source_of[w["work_id"]] = (ly.candidate_id, ly.n_lines) if ly else (None, 0)
    lyr.clear()

    kw = {"batch_size": args.batch_size}
    if args.device:
        kw["device"] = args.device
    backend = make_backend(args.backend, **kw) if args.backend == "nli" else make_backend("claude")
    fallback: NliClassifier | None = None
    todo: list[Item] = []
    skipped = 0
    for it in items:
        row = export.cached(conn, it.work_id)
        if (not args.force and row is not None and row["text_sha256"] == it.sha256()
                and row["model"] == backend.model and row["backend"] == backend.name):
            skipped += 1
            it.lines = None          # nothing more to do with this text
            continue
        todo.append(it)
    _log(f"themes: {len(items)} works ({sum(1 for c, _ in source_of.values() if c is not None)} with lyrics);"
         f" {skipped} cached, {len(todo)} to classify with {backend.name}")
    t1 = time.monotonic()
    done = fell_back = 0
    if todo:
        backend.load()
        _log(f"themes: classifier {backend.model} on {backend.device_name()}")
    for b in range(0, len(todo), args.batch_songs):
        batch = todo[b:b + args.batch_songs]
        dists = list(backend.classify(batch))
        used = [backend] * len(batch)
        redo = [i for i, d in enumerate(dists) if d is None]
        if redo:                                  # Claude refusal/failure -> NLI for those songs
            fallback = fallback or make_backend("nli", **kw)
            for i, d in zip(redo, fallback.classify([batch[i] for i in redo])):
                dists[i], used[i] = d, fallback
            fell_back += len(redo)
        for it, d, u in zip(batch, dists, used):
            cand, n_lines = source_of[it.work_id]
            export.write_song(conn, it.work_id, text_source=it.text_source, candidate_id=cand,
                              text_sha256=it.sha256(), n_lines=n_lines, model=u.model, backend=u.name, dist=d)
            it.lines = None           # drop the text as soon as the song is stored
        conn.commit()
        done += len(batch)
        el = time.monotonic() - t1
        _log(f"themes: classified {done}/{len(todo)} ({el:.0f} s, {el / max(done, 1):.2f} s/song)")
    items.clear()
    return {"read": rstats.as_dict(), "read_seconds": round(read_s, 1), "classified": done, "cached": skipped,
            "classify_seconds": round(time.monotonic() - t1, 1), "backend": backend.name, "model": backend.model,
            "device": backend.device_name() if todo else None, "fallback_to_nli": fell_back}


# ------------------------------------------------------------------ validation
def validate(conn: sqlite3.Connection, work_ids: list[str], *, title_only: bool = True,
             clf: NliClassifier | None = None) -> dict:
    songs = [s for s in validation.load() if s.work_id in set(work_ids)]
    dists = export.stored_distributions(conn, {s.work_id for s in songs})
    sources = {r[0]: r[1] for r in conn.execute("SELECT work_id, text_source FROM song_text")}
    out = {"n_songs": len(songs), "as_run": validation.report(dists, sources, songs),
           "confusion": validation.confusion(dists, songs)}
    if title_only and songs:
        clf = clf or NliClassifier()
        titles = {r[0]: r[1] for r in conn.execute("SELECT work_id, title FROM work")}
        items = [Item(s.work_id, titles[s.work_id], None) for s in songs]
        td = dict(zip([s.work_id for s in songs], clf.classify(items)))
        out["title_only"] = validation.report(td, {w: "title" for w in td}, songs)
    return out


def tune_command(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    songs = validation.load()
    ids = [s.work_id for s in songs]
    titles = {r[0]: r[1] for r in conn.execute("SELECT work_id, title FROM work")}
    lyr, _ = lyrics.read(conn, lyrics.plan(conn, ids), web=not args.no_web, progress=_log)
    items = [Item(w, titles[w], list(lyr[w].lines) if w in lyr else None) for w in ids]
    lyr.clear()
    clf = NliClassifier(batch_size=args.batch_size, **({"device": args.device} if args.device else {}))
    clf.load()
    res = validation.tune(clf, items, songs, progress=_log)
    items.clear()
    for name in ("before", "after"):
        _log(f"{name}: {json.dumps(res[name]['config'])}")
        for k in ("all", "lyrics", "title", "holdout", "holdout_lyrics", "holdout_title"):
            _log(f"  as-run {k:15s} {res[name]['as_run'][k]}")
        _log(f"  title-only all      {res[name]['title_only']['all']}")
    return 0


# ------------------------------------------------------------------ singers
def resolve_singers(conn: sqlite3.Connection, work_ids: list[str]) -> tuple[dict[str, dict], str]:
    try:
        from . import singer  # written by the singer engineer
    except ImportError:
        return {}, "unavailable: musichistory/themes/singer.py not present (every song 'unknown')"
    try:
        res = singer.resolve_singers(conn, work_ids) or {}
        return dict(res), "singer.resolve_singers"
    except Exception as exc:  # network etc.: fall back to whatever the singer table holds
        _log(f"themes: singer.resolve_singers failed ({type(exc).__name__}); using the stored singer table")
        try:
            rows = conn.execute("SELECT work_id, gender, source, artist_qid, artist_label FROM singer").fetchall()
        except sqlite3.Error:
            return {}, f"failed ({type(exc).__name__}); no singer table (every song 'unknown')"
        return ({r[0]: {"gender": r[1], "source": r[2], "artist_qid": r[3], "artist_label": r[4]} for r in rows},
                f"singer table (resolve failed: {type(exc).__name__})")


# ------------------------------------------------------------------ run
def run(args: argparse.Namespace) -> int:
    t_start = time.monotonic()
    conn = db.connect()
    export.ensure_schema(conn)
    if args.tune:
        return tune_command(conn, args)
    works = selected_works(conn, args.work, args.limit)
    if not works:
        _log("themes: no selected works (run select first, or check --work ids)")
        return 1
    ids = [w["work_id"] for w in works]
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "works": len(works)}
    if not args.export_only:
        report["classify"] = classify_works(conn, works, args)

    all_ids = [w["work_id"] for w in selected_works(conn)]
    have = set(export.stored_distributions(conn, set(all_ids)))
    export_ids = [w for w in all_ids if w in have]
    rows = {r[0]: r for r in conn.execute("SELECT work_id, text_source, model, backend FROM song_text")}
    backends = Counter(rows[w]["backend"] for w in export_ids)
    models = Counter(rows[w]["model"] for w in export_ids)
    backend_s = "+".join(sorted(backends)) or args.backend
    model_s = models.most_common(1)[0][0] if models else ""

    t_val = time.monotonic()
    val = validate(conn, export_ids)
    report["validation"] = val
    report["validation_seconds"] = round(time.monotonic() - t_val, 1)
    va = val["as_run"]
    _log(f"themes: validation as run {va['all']}; lyrics {va['lyrics']}; title {va['title']}")
    if "title_only" in val:
        _log(f"themes: validation title-only {val['title_only']['all']}")

    singers, singer_source = ({}, "skipped (--no-singer)") if args.no_singer else resolve_singers(conn, export_ids)
    report["singer_source"] = singer_source
    _log(f"themes: singers from {singer_source}: {dict(Counter((singers.get(w) or {}).get('gender') or 'unknown' for w in export_ids))}")

    if not args.no_export:
        missing = len(all_ids) - len(export_ids)
        if missing:
            _log(f"themes: {missing} selected works have no scores yet; exporting the {len(export_ids)} that do")
        meta = {
            "validation_top1": va["all"].get("top1", ""), "validation_top2": va["all"].get("top2", ""),
            "validation_n": va["all"].get("n", 0),
            "validation_lyrics_top1": va["lyrics"].get("top1", ""), "validation_lyrics_top2": va["lyrics"].get("top2", ""),
            "validation_title_top1": va["title"].get("top1", ""), "validation_title_top2": va["title"].get("top2", ""),
            "singer_source": singer_source, "themes": json.dumps([t.label for t in THEMES]),
        }
        if "title_only" in val:
            meta["validation_title_only_top1"] = val["title_only"]["all"].get("top1", "")
            meta["validation_title_only_top2"] = val["title_only"]["all"].get("top2", "")
        out = Path(args.out) if args.out else export.themes_graph_db()
        report["export"] = export.export_graph(conn, export_ids, backend=backend_s, model=model_s, singers=singers,
                                               meta=meta, out=out)
        _log(f"themes: wrote {out} ({report['export']['songs']} songs, {report['export']['by_source']})")

    dists = export.stored_distributions(conn, set(export_ids))
    report["top_theme_counts"] = {THEMES[k - 1].short: v for k, v in sorted(
        Counter(top_themes(dists[w], 1)[0] for w in export_ids).items())}
    report["text_source_counts"] = dict(Counter(rows[w]["text_source"] for w in export_ids))
    ranked = [w for w in (r["work_id"] for r in works) if w in dists][:15]
    titles = {w["work_id"]: w["title"] for w in works}
    report["examples"] = [{"title": titles[w], "text_source": rows[w]["text_source"],
                           "top2": [[THEMES[k - 1].short, round(float(dists[w][k - 1]), 3)] for k in top_themes(dists[w], 2)]}
                          for w in ranked]
    report["seconds"] = round(time.monotonic() - t_start, 1)
    out_r = config.REPORTS / f"themes_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_r.parent.mkdir(parents=True, exist_ok=True)
    out_r.write_text(json.dumps(report, indent=1, default=_json_default), encoding="utf-8")
    _log(f"themes: top themes {report['top_theme_counts']}")
    _log(f"themes: done in {report['seconds']} s; report {out_r}")
    return 0


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    raise TypeError(type(o).__name__)
