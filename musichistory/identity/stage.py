"""``python -m musichistory analyze``: the analyze stage (DESIGN.md §7).

For every work with ``selected = 1`` and a ``selection`` row:

1. copy the chosen candidate's sanitized MIDI to ``data/songs/<work_id>/score.mid``;
2. reuse the select-time slim analysis of that candidate when it was made by the same
   Resonance build from the same bytes (``data/analysis-work/<work_id>/c<id>/``), else run
   PatternPrep; the slim JSON ends up in ``data/songs/<work_id>/analysis.json``;
3. extract the identity and write ``song``, ``key_region``, ``chord_seq``, ``loop`` and
   ``melody_line`` rows (``analysis_ok``/``error`` on the song row);
4. write ``data/normalized/<work_id>.mid`` (target key per region, one TARGET_BPM tempo).

Works run in parallel processes (CPU count - 1); the parent is the only DB writer. The
stage is resumable: a work whose song row is ok for the same candidate, built with the
current ``NORMALIZATION`` and ``TARGET_BPM`` (``song.normalization`` / ``song.target_bpm``),
is skipped unless ``--force``; a row built with other settings (or before they were
recorded) is analyzed again. ``--work-id`` may name works that are not (yet) selected.

After a run, pipeline meta ``analyze_normalization`` and ``analyze_target_bpm`` hold the
settings every ``analysis_ok`` song was built with. When the analyzed songs mix settings
the keys are deleted (and a warning printed), so no consumer can misreport the frame.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .. import config, db
from ..analysis import patternprep
from ..analysis.patternprep import AnalysisError
from . import extract as extractmod
from . import melody
from .normalize_midi import normalize_midi, target_key_signature


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, default=None, help="analyze at most N works (in canon order)")
    p.add_argument("--work-id", action="append", dest="work_ids", default=None,
                   help="only this work (repeatable); may name a work that is not selected")
    p.add_argument("--force", action="store_true", help="re-analyze works that are already done")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--timeout", type=float, default=180.0, help="PatternPrep timeout per song (s)")


def stored_path(p: Path) -> str:
    """Path as stored in the DB: relative to the repo root when possible ('data/...')."""
    try:
        return p.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def resolve(stored: str) -> Path:
    p = Path(stored)
    return p if p.is_absolute() else config.ROOT / p


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- settings
META_NORMALIZATION = "analyze_normalization"
META_TARGET_BPM = "analyze_target_bpm"


def _bpm_text(bpm: float) -> str:
    bpm = float(bpm)
    return str(int(bpm)) if bpm.is_integer() else repr(bpm)


def settings_match(normalization: str | None, target_bpm: float | None) -> bool:
    """True when a song row was built with the current NORMALIZATION and TARGET_BPM."""
    return (normalization == config.NORMALIZATION and target_bpm is not None
            and abs(float(target_bpm) - float(config.TARGET_BPM)) < 1e-6)


def settings_meta(conn: sqlite3.Connection) -> tuple[str, float] | None:
    """Write meta ``analyze_normalization`` / ``analyze_target_bpm`` when every ``analysis_ok``
    song was built with the same settings, else delete both keys and warn. Returns the
    shared (normalization, target_bpm), or None."""
    groups: dict[tuple, int] = {}
    for r in conn.execute("SELECT normalization, target_bpm FROM song WHERE analysis_ok = 1"):
        key = (r[0], None if r[1] is None else round(float(r[1]), 6))
        groups[key] = groups.get(key, 0) + 1
    if len(groups) == 1:
        (norm, bpm), = groups
        if norm is not None and bpm is not None:
            db.set_meta(conn, META_NORMALIZATION, norm)
            db.set_meta(conn, META_TARGET_BPM, _bpm_text(bpm))
            if not settings_match(norm, bpm):
                print(f"  note: every analyzed song uses normalization {norm} / target BPM {_bpm_text(bpm)}, "
                      f"not the current {config.NORMALIZATION} / {_bpm_text(config.TARGET_BPM)}", flush=True)
            return norm, bpm
    conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (META_NORMALIZATION, META_TARGET_BPM))
    conn.commit()
    if groups:
        mix = ", ".join(f"{n or 'unknown'}/{'?' if b is None else _bpm_text(b)} BPM: {c}"
                        for (n, b), c in sorted(groups.items(), key=lambda kv: -kv[1]))
        print(f"  WARNING: analyzed songs mix normalization/target BPM settings ({mix}); meta "
              f"{META_NORMALIZATION}/{META_TARGET_BPM} removed until they agree. Run analyze with the intended "
              "settings (works that are no longer selected need --work-id ... to be redone).", flush=True)
    return None


# --------------------------------------------------------------------------- worker
def _reusable(paths: list[str], commit: str, sha: str | None) -> dict | None:
    """The first select-time slim analysis made by this Resonance build from these bytes."""
    for p in paths:
        try:
            slim = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(slim, dict) and slim.get("slim_version") == patternprep.SLIM_VERSION
                and slim.get("resonance_commit") == commit and (sha is None or slim.get("midi_sha256") in (None, sha))):
            try:
                patternprep.check_slim(slim)
            except AnalysisError:
                continue
            return slim
    return None


def process(job: dict) -> dict:
    """Analyze one work (runs in a worker process; never touches the DB)."""
    t0 = time.monotonic()
    config.TOOLS = Path(job["tools"])
    config.RESONANCE_ROOT = Path(job["resonance_root"])
    res = {"work_id": job["work_id"], "candidate_id": job["candidate_id"], "ok": False, "error": None, "ident": None,
           "reused": False, "score": job["score"], "analysis": job["analysis"], "normalized": job["normalized"],
           "target_bpm": job["target_bpm"]}
    try:
        src = Path(job["sanitized"])
        if not src.exists():
            raise AnalysisError(f"sanitized MIDI missing: {src}")
        score, analysis = Path(job["score"]), Path(job["analysis"])
        score.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, score)
        sha = _sha256(score)
        slim = _reusable(job["reuse"], job["commit"], sha)
        if slim is not None:
            tmp = analysis.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(slim, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
            tmp.replace(analysis)
            res["reused"] = True
        else:
            slim = patternprep.analyze(score, score.parent, lead_track=melody.lead_track_hint(job["features"]),
                                       timeout=job["timeout"])
        ident = extractmod.extract(slim, job["features"], normalization=job["normalization"])
        normalize_midi(score, job["normalized"], ident.regions, target_bpm=job["target_bpm"],
                       key_signature=target_key_signature(ident.mode, job["normalization"]))
        res.update(ok=True, ident=ident)
    except AnalysisError as exc:
        res["error"] = str(exc)[:400]
    except Exception as exc:  # a hostile file must not stop the batch
        res["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        res["trace"] = traceback.format_exc(limit=6)
    res["seconds"] = round(time.monotonic() - t0, 2)
    return res


# --------------------------------------------------------------------------- parent
def plan(conn: sqlite3.Connection, *, work_ids: list[str] | None, limit: int | None, force: bool,
         commit: str) -> tuple[list[dict], int]:
    rows = conn.execute(
        "SELECT w.work_id, w.selected, s.candidate_id, c.sanitized_path, c.features_json, c.sha256"
        " FROM selection s JOIN work w ON w.work_id = s.work_id JOIN candidate c ON c.candidate_id = s.candidate_id"
        " ORDER BY COALESCE(w.canon_rank, 1000000000), w.work_id").fetchall()
    # Done = ok for the same candidate AND built in the current frame and tempo; a row built with
    # other settings would leave a corpus that mixes normalization frames (DESIGN §1.2).
    done = {r["work_id"]: r["candidate_id"] for r in conn.execute(
        "SELECT work_id, candidate_id, normalization, target_bpm FROM song WHERE analysis_ok = 1")
        if settings_match(r["normalization"], r["target_bpm"])}
    select_paths: dict[int, str] = {}
    try:
        for r in conn.execute("SELECT candidate_id, slim_path FROM select_analysis WHERE ok = 1 AND slim_path IS NOT NULL"):
            select_paths[r["candidate_id"]] = r["slim_path"]
    except sqlite3.Error:
        pass  # select's private table is optional
    wanted = set(work_ids or ())
    jobs, skipped = [], 0
    for r in rows:
        wid, cid = r["work_id"], r["candidate_id"]
        if wanted:
            if wid not in wanted:
                continue
        elif not r["selected"]:
            continue
        if not force and done.get(wid) == cid:
            skipped += 1
            continue
        if not r["sanitized_path"]:
            continue
        work = config.ANALYSIS_WORK / wid
        reuse = [work / f"c{cid}" / "analysis.json", work / f"c{cid}" / "slim.json", work / str(cid) / "analysis.json"]
        if cid in select_paths:
            reuse.insert(0, resolve(select_paths[cid]))
        song_dir = config.SONGS / wid
        jobs.append({
            "work_id": wid, "candidate_id": cid, "sanitized": str(resolve(r["sanitized_path"])),
            "features": json.loads(r["features_json"]) if r["features_json"] else None,
            "score": str(song_dir / "score.mid"), "analysis": str(song_dir / "analysis.json"),
            "normalized": str(config.NORMALIZED / f"{wid}.mid"), "reuse": [str(p) for p in reuse],
            "commit": commit, "tools": str(config.TOOLS), "resonance_root": str(config.RESONANCE_ROOT),
            "normalization": config.NORMALIZATION, "target_bpm": config.TARGET_BPM, "timeout": 180.0,
        })
        if limit is not None and len(jobs) >= limit:
            break
    return jobs, skipped


def record(conn: sqlite3.Connection, res: dict) -> None:
    wid = res["work_id"]
    if res["ok"]:
        res["ident"].to_db(conn, wid, commit=False, candidate_id=res["candidate_id"],
                           midi_path=stored_path(Path(res["score"])),
                           normalized_midi_path=stored_path(Path(res["normalized"])),
                           patterns_path=stored_path(Path(res["analysis"])), analysis_ok=1, error=None,
                           normalization=res["ident"].normalization, target_bpm=float(res["target_bpm"]))
    else:
        extractmod.clear(conn, wid)
        conn.execute(
            "INSERT OR REPLACE INTO song(work_id, candidate_id, midi_path, analysis_ok, error, resonance_commit,"
            " analyzed_at) VALUES (?,?,?,0,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (wid, res["candidate_id"], stored_path(Path(res["score"])), res["error"], res.get("commit")))
    conn.commit()


def run(args: argparse.Namespace) -> int:
    conn = db.connect()
    patternprep.ensure_built()
    commit = patternprep.resonance_commit()
    jobs, skipped = plan(conn, work_ids=args.work_ids, limit=args.limit, force=args.force, commit=commit)
    for j in jobs:
        j["timeout"] = args.timeout
    stale = conn.execute("SELECT COUNT(*) FROM song WHERE analysis_ok = 1 AND COALESCE(resonance_commit, '') != ?",
                         (commit,)).fetchone()[0]
    other = sum(1 for r in conn.execute("SELECT normalization, target_bpm FROM song WHERE analysis_ok = 1")
                if not settings_match(r[0], r[1]))
    print(f"analyze: {len(jobs)} to do, {skipped} already done; Resonance {commit[:12]}; "
          f"normalization {config.NORMALIZATION}; target BPM {_bpm_text(config.TARGET_BPM)}; "
          f"workers {args.workers}", flush=True)
    if stale:
        print(f"  note: {stale} analyzed songs used another Resonance build (--force re-analyzes them)")
    if other:
        print(f"  note: {other} analyzed songs were built with another normalization or target BPM "
              "(or before these were recorded); they count as not done and are planned again")
    db.set_meta(conn, "analyze_resonance_commit", commit)
    # Settings meta is rewritten after the run from what the song rows say; until then (or if the
    # run dies) it must not claim a frame the rows may no longer share.
    conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (META_NORMALIZATION, META_TARGET_BPM))
    conn.commit()
    try:
        return _run_jobs(conn, jobs, args, commit)
    finally:
        settings_meta(conn)


def _run_jobs(conn: sqlite3.Connection, jobs: list[dict], args: argparse.Namespace, commit: str) -> int:
    n_ok = n_fail = n_reused = 0
    secs = 0.0
    t0 = time.monotonic()

    def handle(i: int, res: dict) -> None:
        nonlocal n_ok, n_fail, n_reused, secs
        res["commit"] = commit
        record(conn, res)
        secs += res.get("seconds", 0.0)
        if res["ok"]:
            n_ok += 1
            n_reused += res["reused"]
            ident = res["ident"]
            mel = ident.melody.method if ident.melody else "none"
            print(f"[{i}/{len(jobs)}] {res['work_id']} ok {ident.key_name} ({ident.key_confidence:.2f})"
                  f" {ident.native_bpm:g} bpm loops={len(ident.loops)} melody={mel}"
                  f"{' reused' if res['reused'] else ''} {res['seconds']:.1f}s", flush=True)
        else:
            n_fail += 1
            print(f"[{i}/{len(jobs)}] {res['work_id']} FAILED {res['error']}", flush=True)

    if args.workers <= 1 or len(jobs) <= 1:
        for i, job in enumerate(jobs, 1):
            handle(i, process(job))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process, job): job for job in jobs}
            try:
                for i, fut in enumerate(as_completed(futures), 1):
                    try:
                        res = fut.result()
                    except Exception as exc:  # a worker died (e.g. out of memory): record, go on
                        job = futures[fut]
                        res = {"work_id": job["work_id"], "candidate_id": job["candidate_id"], "ok": False,
                               "error": f"worker failed: {type(exc).__name__}: {str(exc)[:200]}",
                               "score": job["score"], "reused": False}
                    handle(i, res)
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                raise
    wall = time.monotonic() - t0
    print(f"analyze: {n_ok} ok ({n_reused} reused select-time analyses), {n_fail} failed, "
          f"{wall:.1f}s wall, {secs / max(1, n_ok + n_fail):.2f}s per song")
    return 1 if jobs and n_ok == 0 else 0
