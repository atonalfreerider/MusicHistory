"""Hand-labelled validation of the theme classifier (DESIGN.md §12).

``tests/themes/validation_set.json`` lists songs of the graph with the theme a listener
would expect from their well-known subject matter: ``primary`` (the expected top theme) and
an acceptable ``secondary`` (or null). Titles, artists and theme ids only - no lyrics. A
stratified third (``split: holdout``) is never used for tuning.

Metrics (per text source, and overall):

* ``top1``     the classifier's top theme is the expected primary;
* ``top1_ok``  the top theme is the primary or the acceptable secondary;
* ``top2``     the primary is among the classifier's two highest themes.

``tune`` compares classifier settings on the ``tune`` split (lyrics are read once and kept
in memory for the whole comparison; entailments are computed once per chunking and
hypothesis set, and aggregation settings are re-applied to them) and reports the chosen
settings on both splits. Only numbers are printed.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from .. import config
from .classify import DEFAULT_CONFIG, Item, NliClassifier, NliConfig, top_themes

VALIDATION_SET = config.ROOT / "tests" / "themes" / "validation_set.json"


@dataclass(frozen=True)
class ValSong:
    work_id: str
    title: str
    artist: str
    primary: int
    secondary: int | None
    split: str
    instrumental: bool = False


def load(path: Path = VALIDATION_SET) -> list[ValSong]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return [ValSong(s["work_id"], s["title"], s["artist"], int(s["primary"]),
                    None if s.get("secondary") is None else int(s["secondary"]), s.get("split", "tune"),
                    bool(s.get("instrumental", False))) for s in doc["songs"]]


def score(dists: dict[str, np.ndarray], songs: list[ValSong]) -> dict:
    """Accuracy of ``work_id -> distribution`` on ``songs`` (songs without a distribution are skipped)."""
    n = t1 = t1ok = t2 = 0
    rank_sum = 0.0
    for s in songs:
        d = dists.get(s.work_id)
        if d is None:
            continue
        n += 1
        top = top_themes(d, 10)
        t1 += top[0] == s.primary
        t1ok += top[0] in (s.primary, s.secondary)
        t2 += s.primary in top[:2]
        rank_sum += top.index(s.primary) + 1
    if n == 0:
        return {"n": 0}
    return {"n": n, "top1": round(t1 / n, 3), "top1_ok": round(t1ok / n, 3), "top2": round(t2 / n, 3),
            "mean_rank": round(rank_sum / n, 2)}


def report(dists: dict[str, np.ndarray], sources: dict[str, str], songs: list[ValSong]) -> dict:
    """Accuracy overall, by text source ('lyrics' / 'title') and by split."""
    out = {"all": score(dists, songs)}
    for src in ("lyrics", "title"):
        out[src] = score(dists, [s for s in songs if sources.get(s.work_id) == src])
    for split in ("tune", "holdout"):
        sub = [s for s in songs if s.split == split]
        out[split] = score(dists, sub)
        for src in ("lyrics", "title"):
            out[f"{split}_{src}"] = score(dists, [s for s in sub if sources.get(s.work_id) == src])
    return out


def confusion(dists: dict[str, np.ndarray], songs: list[ValSong]) -> dict[int, dict[int, int]]:
    """expected primary -> predicted top theme -> count."""
    out: dict[int, dict[int, int]] = {}
    for s in songs:
        d = dists.get(s.work_id)
        if d is None:
            continue
        p = top_themes(d, 1)[0]
        out.setdefault(s.primary, {}).setdefault(p, 0)
        out[s.primary][p] += 1
    return out


# ------------------------------------------------------------------ tuning
# The settings the first version shipped with: the "before" of every tuning report.
BASELINE_CONFIG = NliConfig(hypotheses="v1", chunk_lines=4, chunk_stride=2, hyp_combine="max", top_k=3,
                            title_template="{title}", other_tau=0.5, title_other_tau=None)

AGG_GRID = {
    "hyp_combine": ("max", "mean"),
    "top_k": (1, 2, 3, 5, 8),
    "other_tau": (0.4, 0.5, 0.6, 0.7, 0.8, 0.9),
}
PREMISE_GRID = [
    # (hypotheses, chunk_lines, chunk_stride, title_template)
    ("v1", 4, 2, "{title}"),
    ("v2", 4, 2, "{title}"),
    ("v2", 6, 3, "{title}"),
    ("v3", 4, 2, "{title}"),
    ("v3", 6, 3, "{title}"),
    ("v3", 8, 4, "{title}"),
    ("v3", 6, 3, 'A song called "{title}".'),
]
TITLE_TAU_GRID = (None, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4)


def _key(r: dict) -> tuple:
    return (r.get("top1", 0), r.get("top1_ok", 0), r.get("top2", 0), -r.get("mean_rank", 99))


def tune(clf: NliClassifier, items: list[Item], songs: list[ValSong], *,
         premise_grid: list[tuple] = PREMISE_GRID, agg_grid: dict = AGG_GRID,
         title_tau_grid: tuple = TITLE_TAU_GRID, baseline: NliConfig = BASELINE_CONFIG,
         progress: Callable[[str], None] = print) -> dict:
    """Two-stage grid on the tune split; returns before/after metrics on both splits.

    Stage A picks hypotheses, chunking, title template and aggregation on the tune songs that
    have lyrics. Stage B picks the title-only 'Other' threshold on every tune song classified
    from its title alone. The holdout split is only ever reported.
    """
    by_id = {s.work_id: s for s in songs}
    items = [it for it in items if it.work_id in by_id]
    titles = [Item(it.work_id, it.title, None) for it in items]
    tune_ids = {s.work_id for s in songs if s.split == "tune"}
    tune_lyric = [by_id[it.work_id] for it in items if it.lines is not None and it.work_id in tune_ids]
    tune_all = [s for s in songs if s.split == "tune"]
    sources = {it.work_id: it.text_source for it in items}
    cache: dict[tuple, tuple[list[np.ndarray], list[np.ndarray]]] = {}
    t0 = time.monotonic()

    def entail(cfg: NliConfig) -> tuple[list[np.ndarray], list[np.ndarray]]:
        key = (cfg.hypotheses, cfg.chunk_lines, cfg.chunk_stride, cfg.title_template)
        if key not in cache:
            c = clf.with_config(cfg)
            cache[key] = (c.entailments(items), c.entailments(titles))
        return cache[key]

    def dists(cfg: NliConfig, title_only: bool = False) -> dict[str, np.ndarray]:
        Es, Ts = entail(cfg)
        cc = clf.with_config(cfg)
        if title_only:
            return {it.work_id: cc.aggregate(E, True) for it, E in zip(titles, Ts)}
        return {it.work_id: cc.aggregate(E, it.lines is None) for it, E in zip(items, Es)}

    stage_a = []
    for hyp, cl, cs, tt in premise_grid:
        base_cfg = replace(baseline, hypotheses=hyp, chunk_lines=cl, chunk_stride=cs, title_template=tt)
        entail(base_cfg)
        for combo in itertools.product(*agg_grid.values()):
            cfg = replace(base_cfg, **dict(zip(agg_grid, combo)))
            stage_a.append((cfg, score(dists(cfg), tune_lyric)))
        progress(f"themes tune: {hyp} chunks {cl}/{cs} title {'plain' if tt == '{title}' else 'sentence'}"
                 f" ({time.monotonic() - t0:.0f} s)")
    best_a, best_a_row = max(stage_a, key=lambda x: _key(x[1]))
    stage_b = [(replace(best_a, title_other_tau=tau), None) for tau in title_tau_grid]
    stage_b = [(cfg, score(dists(cfg, title_only=True), tune_all)) for cfg, _ in stage_b]
    best, best_b_row = max(stage_b, key=lambda x: _key(x[1]))

    def full(cfg: NliConfig) -> dict:
        d = dists(cfg)
        return {"config": {k: v for k, v in cfg.__dict__.items()},
                "as_run": report(d, sources, songs), "confusion": confusion(d, songs),
                "title_only": report(dists(cfg, title_only=True), {w: "title" for w in by_id}, songs)}

    return {
        "n_settings": len(stage_a) + len(stage_b), "seconds": round(time.monotonic() - t0, 1),
        "before": full(baseline), "after": full(best),
        "stage_a_best_tune_lyrics": best_a_row, "stage_b_best_tune_title_only": best_b_row,
        "top_stage_a": [{"config": {k: v for k, v in cfg.__dict__.items() if k in (
            "hypotheses", "chunk_lines", "chunk_stride", "hyp_combine", "top_k", "other_tau", "title_template")},
                         "tune_lyrics": r} for cfg, r in sorted(stage_a, key=lambda x: _key(x[1]), reverse=True)[:8]],
        "stage_b": [{"title_other_tau": cfg.title_other_tau, "tune_title_only": r} for cfg, r in stage_b],
    }
