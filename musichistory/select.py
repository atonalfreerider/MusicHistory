"""``python -m musichistory select``: score candidates, choose one MIDI per song, fix the set.

DESIGN.md §6:

1. **Static quality** (0..1) of every valid candidate from its ``features_json``.
2. The best ``--n-analyze`` (4) candidates per work with distinct sanitized content are
   analyzed with PatternPrep and turned into identities through the analyze stage's
   Python API (``musichistory.analysis.patternprep``, ``musichistory.identity``).
   Files that differ only in stripped text sanitize to identical bytes; analyzing copies
   would inflate consensus, so only one per SHA-256 is analyzed.
3. **Consensus**: mean pairwise chord/melody agreement with the work's other analyzed
   candidates (the medoid of agreeing transcriptions is most likely right), and
   **Hooktheory** agreement where annotations exist.
4. ``total = 0.35 quality + 0.40 consensus + 0.25 hooktheory`` (weights re-normalized over
   the terms present); the best analyzed candidate is chosen (``selection`` row).
5. **Final set**: works in ``canon_rank`` order with a successful choice until
   ``TARGET_SONGS``, with a floor of 5 per year from 1950 to the last complete year minus
   one (short years filled from their next-best works; the lowest-ranked songs of
   over-full years dropped). Sets ``work.selected``.
6. **Source comparison report**: ``data/reports/source_comparison.json`` and ``.md``.

Slim analyses are cached per candidate in ``data/analysis-work/<work_id>/c<id>/slim.json``
(and in the private ``select_analysis`` table), so a re-run only analyzes new candidates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import config, db
from .sources import base as sources_base
from .sources import hooktheory

WEIGHTS = {"quality": 0.35, "consensus": 0.40, "hooktheory": 0.25}
N_ANALYZE = 4
FLOOR_PER_YEAR = 5
FLOOR_FIRST_YEAR = 1950

QUALITY_WEIGHTS = {"tracks": 0.15, "drums": 0.10, "melody": 0.20, "tempo": 0.10, "duration": 0.15,
                   "lmd": 0.20, "match": 0.10}

SCHEMA = """
CREATE TABLE IF NOT EXISTS select_analysis(   -- PatternPrep runs made during selection
  candidate_id INTEGER PRIMARY KEY, work_id TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT,
  slim_path TEXT, lead_track INTEGER, seconds REAL, analyzed_at TEXT);
"""


# ------------------------------------------------------------------ static quality
_MEDLEY = re.compile(r"medley|megamix|mash ?-?up|potpourri|mix of", re.I)
_RINGTONE = re.compile(r"ringtone|polyphon", re.I)


def static_quality(f: dict, cand: dict) -> tuple[float, dict]:
    """Score 0..1 and its components for one candidate (``cand`` = candidate row fields)."""
    pitched = [t for t in f.get("tracks", []) if not t["is_drum"] and t["n_notes"] >= 16]
    n_p = len(pitched)
    comp: dict[str, Any] = {}
    comp["tracks"] = min(1.0, n_p / 4.0)
    comp["drums"] = 1.0 if f.get("has_drums") else 0.0
    named = {t["role"] for t in pitched if t.get("role_src") == "name"}
    if named & {"vocal", "melody"} or f.get("lyric_melody_track") is not None:
        comp["melody"] = 1.0
    elif "lead" in named:
        comp["melody"] = 0.8
    elif any(t["role"] == "lead" for t in pitched):
        comp["melody"] = 0.6
    else:
        comp["melody"] = 0.3
    tp = f.get("tempo") or {}
    bpm, stable = tp.get("median_bpm", 120.0), tp.get("stable_fraction", 1.0)
    comp["tempo"] = round((1.0 if 50 <= bpm <= 220 else 0.4) * (0.5 + 0.5 * stable), 4)
    d = f.get("duration_s", 0.0)
    comp["duration"] = 1.0 if 90 <= d <= 480 else (0.6 if 60 <= d <= 600 else 0.2)
    s = cand.get("lmd_match_score")
    comp["lmd"] = 0.0 if s is None else (1.0 if s >= 0.7 else round(0.5 + 2.5 * max(0.0, s - 0.5), 4))
    comp["match"] = 1.0 if cand.get("match_class") == "accept" else 0.7
    q = sum(QUALITY_WEIGHTS[k] * comp[k] for k in QUALITY_WEIGHTS) / sum(QUALITY_WEIGHTS.values())
    flags, pen = [], 1.0
    name = cand.get("orig_name") or ""
    if f.get("karaoke") and n_p <= 2:
        pen *= 0.6
        flags.append("karaoke_thin")
    if pitched and all(t["family"] == "piano" for t in pitched) and not f.get("has_drums"):
        pen *= 0.6
        flags.append("piano_only")
    if _RINGTONE.search(name):
        pen *= 0.7
        flags.append("ringtone")
    if d < 60:
        pen *= 0.5
        flags.append("short")
    if _MEDLEY.search(name) and not _MEDLEY.search(cand.get("title") or ""):
        pen *= 0.4
        flags.append("medley")
    comp["penalties"] = flags
    return round(q * pen, 4), comp


def lead_track(f: dict) -> int | None:
    """Track PatternPrep should treat as the lead vocal, when the file says so."""
    tracks = [t for t in f.get("tracks", []) if not t["is_drum"]]
    for role in ("vocal", "melody"):
        named = [t for t in tracks if t["role"] == role and t.get("role_src") == "name"]
        if named:
            return max(named, key=lambda t: t["n_notes"])["index"]
    if f.get("lyric_melody_track") is not None:
        return int(f["lyric_melody_track"])
    named = [t for t in tracks if t["role"] == "lead" and t.get("role_src") == "name"]
    return max(named, key=lambda t: t["n_notes"])["index"] if named else None


def combine(quality: float | None, consensus: float | None, hook: float | None) -> float | None:
    terms = {"quality": quality, "consensus": consensus, "hooktheory": hook}
    present = {k: v for k, v in terms.items() if v is not None}
    if not present:
        return None
    wsum = sum(WEIGHTS[k] for k in present)
    return round(sum(WEIGHTS[k] * v for k, v in present.items()) / wsum, 4)


# ------------------------------------------------------------------ analysis engine
class Engine(Protocol):
    def analyze(self, midi_path: Path, workdir: Path, lead_track: int | None) -> dict: ...
    def extract(self, slim: dict, features: dict) -> Any: ...
    def chord_agreement(self, a: Any, b: Any) -> float: ...
    def melody_agreement(self, a: Any, b: Any) -> float: ...
    def hooktheory_agreement(self, ident: Any, sections: list[dict]) -> float | None: ...


class AnalyzeStageEngine:
    """The analyze stage's documented Python API (DESIGN.md §7)."""

    def __init__(self, timeout: float = 180.0) -> None:
        from .analysis import patternprep
        from .identity import compare, extract

        self.pp, self.ex, self.cmp, self.timeout = patternprep, extract, compare, timeout
        self.pp.ensure_built()
        self.cache_key = f"resonance={self.pp.resonance_commit()}"

    def analyze(self, midi_path: Path, workdir: Path, lead_track: int | None) -> dict:
        return self.pp.analyze(midi_path, workdir, lead_track=lead_track, timeout=self.timeout)

    def extract(self, slim: dict, features: dict) -> Any:
        return self.ex.extract(slim, features)

    def chord_agreement(self, a: Any, b: Any) -> float:
        return self.cmp.chord_agreement(a, b)

    def melody_agreement(self, a: Any, b: Any) -> float:
        return self.cmp.melody_agreement(a, b)

    def hooktheory_agreement(self, ident: Any, sections: list[dict]) -> float | None:
        return self.cmp.hooktheory_agreement(ident, sections)


@dataclass
class Cand:
    candidate_id: int
    work_id: str
    source: str
    orig_name: str
    sha256: str | None
    path: Path
    features: dict
    quality: float
    ident: Any = None
    ok: bool = False
    error: str | None = None
    consensus: float | None = None
    hook: float | None = None
    total: float | None = None


@dataclass
class WorkResult:
    work_id: str
    chosen: Cand | None
    analyzed: list[Cand] = field(default_factory=list)
    n_candidates: int = 0
    n_sources: int = 0
    reason: str = ""


def _analyze_one(engine: Engine, c: Cand, reanalyze: bool) -> tuple[Cand, dict | None, float]:
    """Run (or load) the slim analysis and identity for one candidate (thread worker)."""
    workdir = config.ANALYSIS_WORK / c.work_id / f"c{c.candidate_id}"
    slim_file, key_file = workdir / "slim.json", workdir / "slim.key"
    # The cache is valid only for the same engine version (Resonance commit, slim version)
    # and the same sanitized file.
    key = f"{getattr(engine, 'cache_key', '')}|{c.sha256}"
    t0 = time.monotonic()
    slim = None
    if slim_file.exists() and not reanalyze and key_file.exists() and key_file.read_text(encoding="utf-8") == key:
        try:
            slim = json.loads(slim_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            slim = None
    try:
        if slim is None:
            workdir.mkdir(parents=True, exist_ok=True)
            slim = engine.analyze(c.path, workdir, lead_track(c.features))
            slim_file.write_text(json.dumps(slim, separators=(",", ":")), encoding="utf-8")
            key_file.write_text(key, encoding="utf-8")
        c.ident = engine.extract(slim, c.features)
        c.ok = c.ident is not None
    except Exception as exc:  # AnalysisError or anything a broken file triggers
        c.ok, c.error = False, f"{type(exc).__name__}: {str(exc)[:200]}"
    return c, slim, time.monotonic() - t0


def _pair(engine: Engine, a: Cand, b: Cand) -> float | None:
    vals = []
    for fn in (engine.chord_agreement, engine.melody_agreement):
        try:
            v = fn(a.ident, b.ident)
        except Exception:
            v = None
        if v is not None:
            vals.append(float(v))
    return sum(vals) / len(vals) if vals else None


def score_work(engine: Engine, cands: list[Cand], sections: list[dict]) -> None:
    """Fill consensus, Hooktheory agreement and totals for the analyzed candidates of a work."""
    ok = [c for c in cands if c.ok]
    pairs: dict[tuple[int, int], float | None] = {}
    for i, a in enumerate(ok):
        for b in ok[i + 1:]:
            pairs[(a.candidate_id, b.candidate_id)] = pairs[(b.candidate_id, a.candidate_id)] = _pair(engine, a, b)
    for c in ok:
        vals = [v for (x, _), v in pairs.items() if x == c.candidate_id and v is not None]
        c.consensus = round(sum(vals) / len(vals), 4) if vals else None
        c.hook = None
        if sections:
            try:
                h = engine.hooktheory_agreement(c.ident, sections)
                c.hook = None if h is None else round(float(h), 4)
            except Exception:
                c.hook = None
        c.total = combine(c.quality, c.consensus, c.hook)


def choose(cands: list[Cand]) -> Cand | None:
    ok = [c for c in cands if c.ok and c.total is not None]
    if not ok:
        return None
    return max(ok, key=lambda c: (c.total, c.quality, -c.candidate_id))


def reason_for(c: Cand, n_analyzed: int, n_sources: int) -> str:
    parts = [f"q={c.quality:.2f}"]
    if c.consensus is not None:
        parts.append(f"cons={c.consensus:.2f}")
    if c.hook is not None:
        parts.append(f"ht={c.hook:.2f}")
    return (f"{c.source} '{c.orig_name[:60]}' total={c.total:.3f} ({', '.join(parts)}; "
            f"best of {n_analyzed} analyzed, {n_sources} sources)")


# ------------------------------------------------------------------ final set
def final_set(works: list[tuple[str, int | None, int | None]], target: int, floor: int, first: int,
              last: int) -> list[str]:
    """works: (work_id, canon_rank, year) with a successful choice. Returns selected ids."""
    order = sorted(works, key=lambda w: (w[1] is None, w[1] or 0, w[0]))
    chosen = order[:target]
    ids = {w[0] for w in chosen}
    count: dict[int | None, int] = {}
    for w in chosen:
        count[w[2]] = count.get(w[2], 0) + 1
    for y in range(first, last + 1):
        need = floor - count.get(y, 0)
        if need <= 0:
            continue
        for w in order:
            if need <= 0:
                break
            if w[2] == y and w[0] not in ids:
                chosen.append(w)
                ids.add(w[0])
                count[y] = count.get(y, 0) + 1
                need -= 1

    def protected(w: tuple[str, int | None, int | None]) -> bool:
        return w[2] is not None and first <= w[2] <= last and count.get(w[2], 0) <= floor

    while len(chosen) > target:
        victim = next((w for w in sorted(chosen, key=lambda w: (w[1] is None, w[1] or 0, w[0]), reverse=True)
                       if not protected(w)), None)
        if victim is None:
            break
        chosen.remove(victim)
        count[victim[2]] -= 1
    return [w[0] for w in chosen]


def default_floor_last(today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return today.year - 2  # last complete year minus one


# ------------------------------------------------------------------ report
def source_report(conn: sqlite3.Connection) -> dict:
    pool = {r[0]: r[1] for r in conn.execute("SELECT work_id, work_year FROM work WHERE in_pool = 1")}
    decade = lambda y: None if y is None else (y // 10) * 10  # noqa: E731
    pool_by_dec: dict[str, int] = {}
    for y in pool.values():
        k = str(decade(y))
        pool_by_dec[k] = pool_by_dec.get(k, 0) + 1
    out: dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pool_works": len(pool),
                           "pool_by_decade": pool_by_dec, "sources": {}}
    dups = {r[0]: r[1] for r in conn.execute(
        "SELECT source, COUNT(*) FROM fetch_attempt WHERE status='duplicate' GROUP BY source")} \
        if _has_table(conn, "fetch_attempt") else {}
    rows = conn.execute(
        "SELECT c.source, c.work_id, c.valid, c.analyzed, c.chosen, c.quality_score, c.consensus_score,"
        " c.hooktheory_score, w.work_year FROM candidate c JOIN work w USING(work_id)").fetchall()
    for src in sorted({r[0] for r in rows}):
        rs = [r for r in rows if r[0] == src]
        mean = lambda i: (lambda v: round(sum(v) / len(v), 4) if v else None)(  # noqa: E731
            [r[i] for r in rs if r[i] is not None and r[2]])
        cov: dict[str, dict[str, int]] = {}
        for k in pool_by_dec:
            wv = {r[1] for r in rs if r[2] and str(decade(pool.get(r[1]))) == k}
            wc = {r[1] for r in rs if r[4] and str(decade(pool.get(r[1]))) == k}
            cov[k] = {"works_valid": len(wv), "works_chosen": len(wc), "pool": pool_by_dec[k]}
        out["sources"][src] = {
            "candidates": len(rs), "valid": sum(r[2] for r in rs), "analyzed": sum(r[3] for r in rs),
            "chosen": sum(r[4] for r in rs), "duplicates_skipped": dups.get(src, 0),
            "works_with_valid": len({r[1] for r in rs if r[2]}),
            "mean_quality": mean(5), "mean_consensus": mean(6), "mean_hooktheory": mean(7),
            "coverage_by_decade": cov,
        }
    sel = conn.execute("SELECT COUNT(*) FROM selection").fetchone()[0]
    out["works_chosen"] = sel
    out["works_selected"] = conn.execute("SELECT COUNT(*) FROM work WHERE selected = 1").fetchone()[0]
    return out


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def write_report(rep: dict) -> tuple[Path, Path]:
    d = config.DATA / "reports"
    d.mkdir(parents=True, exist_ok=True)
    jp, mp = d / "source_comparison.json", d / "source_comparison.md"
    jp.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    fmt = lambda v: "-" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))  # noqa: E731
    lines = [f"# MIDI source comparison ({rep['generated_at']})", "",
             f"Pool works: {rep['pool_works']}; works with a chosen MIDI: {rep['works_chosen']};"
             f" final selection: {rep['works_selected']}.", "",
             "| source | candidates | valid | dup. skipped | works | analyzed | chosen | quality | consensus"
             " | Hooktheory |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for s, v in rep["sources"].items():
        lines.append(f"| {s} | {v['candidates']} | {v['valid']} | {v['duplicates_skipped']} | {v['works_with_valid']}"
                     f" | {v['analyzed']} | {v['chosen']} | {fmt(v['mean_quality'])} | {fmt(v['mean_consensus'])}"
                     f" | {fmt(v['mean_hooktheory'])} |")
    decs = sorted(rep["pool_by_decade"], key=lambda k: (k == "None", k))
    lines += ["", "Coverage by decade (works with a valid candidate / chosen, of pool works):", "",
              "| source | " + " | ".join(f"{k}s" for k in decs) + " |", "|---|" + "---|" * len(decs)]
    for s, v in rep["sources"].items():
        cells = [f"{v['coverage_by_decade'][k]['works_valid']}/{v['coverage_by_decade'][k]['works_chosen']}"
                 f" of {v['coverage_by_decade'][k]['pool']}" for k in decs]
        lines.append(f"| {s} | " + " | ".join(cells) + " |")
    mp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jp, mp


# ------------------------------------------------------------------ stage
def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--work", action="append", help="only this work_id (repeatable)")
    p.add_argument("--limit", type=int, help="only the first N pooled works by canon_rank")
    p.add_argument("--n-analyze", type=int, default=N_ANALYZE)
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--timeout", type=float, default=180.0, help="PatternPrep timeout per file (s)")
    p.add_argument("--reanalyze", action="store_true", help="ignore cached slim analyses")
    p.add_argument("--target", type=int, default=config.TARGET_SONGS)
    p.add_argument("--floor", type=int, default=FLOOR_PER_YEAR)
    p.add_argument("--floor-first", type=int, default=FLOOR_FIRST_YEAR)
    p.add_argument("--floor-last", type=int, default=default_floor_last())
    p.add_argument("--report-only", action="store_true")


def _load_cands(conn: sqlite3.Connection, work_id: str, title: str) -> list[Cand]:
    out = []
    for r in conn.execute(
        "SELECT candidate_id, source, orig_name, sha256, sanitized_path, features_json, match_class,"
        " lmd_match_score FROM candidate WHERE work_id=? AND valid=1 AND sanitized_path IS NOT NULL", (work_id,)):
        f = json.loads(r["features_json"] or "{}")
        q, _ = static_quality(f, {"orig_name": r["orig_name"], "match_class": r["match_class"],
                                  "lmd_match_score": r["lmd_match_score"], "title": title})
        out.append(Cand(r["candidate_id"], work_id, r["source"], r["orig_name"] or "", r["sha256"],
                        sources_base.resolve(r["sanitized_path"]), f, q))
    return out


def select_work(engine: Engine, conn: sqlite3.Connection, work_id: str, title: str, *, n_analyze: int,
                workers: int, reanalyze: bool, analyze: Callable = _analyze_one) -> WorkResult:
    cands = _load_cands(conn, work_id, title)
    res = WorkResult(work_id, None, n_candidates=len(cands), n_sources=len({c.source for c in cands}))
    for c in cands:
        conn.execute("UPDATE candidate SET quality_score=? WHERE candidate_id=?", (c.quality, c.candidate_id))
    if not cands:
        res.reason = "no valid candidates"
        return res
    ranked = sorted(cands, key=lambda c: (-c.quality, c.candidate_id))
    distinct: list[Cand] = []
    seen: set[str | None] = set()
    for c in ranked:
        key = c.sha256 or f"id{c.candidate_id}"
        if key not in seen:
            seen.add(key)
            distinct.append(c)
    batch_start = 0
    while batch_start < len(distinct):
        batch = distinct[batch_start:batch_start + n_analyze]
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batch)))) as pool:
            done = list(pool.map(lambda c: analyze(engine, c, reanalyze), batch))
        for c, slim, secs in done:
            res.analyzed.append(c)
            conn.execute(
                "INSERT OR REPLACE INTO select_analysis(candidate_id, work_id, ok, error, slim_path, lead_track,"
                " seconds, analyzed_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
                (c.candidate_id, work_id, int(c.ok), c.error,
                 sources_base.stored_path(config.ANALYSIS_WORK / work_id / f"c{c.candidate_id}" / "slim.json")
                 if slim is not None else None, lead_track(c.features), round(secs, 2)))
        if any(c.ok for c in res.analyzed):
            break
        batch_start += n_analyze  # every analysis failed: try the next-best candidates once
        if batch_start >= 2 * n_analyze:
            break
    sections = hooktheory.sections_for(conn, work_id) if _has_table(conn, "hooktheory_match") else []
    score_work(engine, res.analyzed, sections)
    for c in res.analyzed:
        conn.execute("UPDATE candidate SET analyzed=1, consensus_score=?, hooktheory_score=?, total_score=?"
                     " WHERE candidate_id=?", (c.consensus, c.hook, c.total, c.candidate_id))
    res.chosen = choose(res.analyzed)
    conn.execute("UPDATE candidate SET chosen=0 WHERE work_id=?", (work_id,))
    conn.execute("DELETE FROM selection WHERE work_id=?", (work_id,))
    n_ok = sum(c.ok for c in res.analyzed)
    if res.chosen is not None:
        res.reason = reason_for(res.chosen, n_ok, res.n_sources)
        conn.execute("UPDATE candidate SET chosen=1 WHERE candidate_id=?", (res.chosen.candidate_id,))
        conn.execute("INSERT INTO selection(work_id, candidate_id, n_candidates, n_sources, n_analyzed, reason)"
                     " VALUES (?,?,?,?,?,?)", (work_id, res.chosen.candidate_id, res.n_candidates, res.n_sources,
                                               n_ok, res.reason))
    else:
        errs = {c.error for c in res.analyzed if c.error}
        res.reason = f"analysis failed for {len(res.analyzed)} candidates: {sorted(errs)[:2]}"
    conn.commit()
    return res


def apply_final_set(conn: sqlite3.Connection, target: int, floor: int, first: int, last: int) -> list[str]:
    rows = conn.execute("SELECT s.work_id, w.canon_rank, w.work_year FROM selection s JOIN work w USING(work_id)"
                        " WHERE w.in_pool = 1").fetchall()
    ids = final_set([(r[0], r[1], r[2]) for r in rows], target, floor, first, last)
    conn.execute("UPDATE work SET selected = 0")
    conn.executemany("UPDATE work SET selected = 1 WHERE work_id = ?", [(i,) for i in ids])
    conn.commit()
    return ids


def run(args: argparse.Namespace, engine: Engine | None = None) -> int:
    conn = db.connect()
    conn.executescript(SCHEMA)
    sources_base.ensure_schema(conn)
    if not args.report_only:
        if engine is None:
            try:
                engine = AnalyzeStageEngine(timeout=args.timeout)
            except Exception as exc:  # module missing, or PatternPrep cannot be built
                print(f"select: the analyze stage's API is not available ({type(exc).__name__}: {exc})")
                return 2
        sql = "SELECT work_id, title FROM work"
        params: list = []
        if args.work:
            sql += f" WHERE work_id IN ({','.join('?' * len(args.work))})"
            params = list(args.work)
        else:
            sql += " WHERE in_pool = 1"
        sql += " ORDER BY canon_rank IS NULL, canon_rank"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        works = conn.execute(sql, params).fetchall()
        t0 = time.monotonic()
        n_chosen = 0
        for i, w in enumerate(works, 1):
            res = select_work(engine, conn, w["work_id"], w["title"], n_analyze=args.n_analyze,
                              workers=args.workers, reanalyze=args.reanalyze)
            n_chosen += res.chosen is not None
            print(f"[{i}/{len(works)}] {w['work_id']} {w['title'][:40]}: {res.reason}", flush=True)
        print(f"select: chose a MIDI for {n_chosen}/{len(works)} works in {time.monotonic() - t0:.0f} s")
        ids = apply_final_set(conn, args.target, args.floor, args.floor_first, args.floor_last)
        print(f"select: final set {len(ids)} works (target {args.target}, floor {args.floor}/year "
              f"{args.floor_first}-{args.floor_last})")
    jp, mp = write_report(source_report(conn))
    print(f"select: report {jp} and {mp.name}")
    return 0
