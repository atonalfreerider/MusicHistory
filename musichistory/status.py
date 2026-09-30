"""``python -m musichistory status``: one line per stage with its progress."""

from __future__ import annotations

import argparse
import sqlite3

from . import config, db


def add_arguments(p: argparse.ArgumentParser) -> None:
    pass


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    try:
        return conn.execute(sql).fetchone()[0] or 0
    except sqlite3.Error:
        return 0


def run(args: argparse.Namespace) -> int:
    conn = db.connect()
    rows = [
        ("canon: works", "SELECT COUNT(*) FROM work"),
        ("canon: pool", "SELECT COUNT(*) FROM work WHERE in_pool = 1"),
        ("fetch: candidates", "SELECT COUNT(*) FROM candidate"),
        ("fetch: valid candidates", "SELECT COUNT(*) FROM candidate WHERE valid = 1"),
        ("fetch: pool works with a valid candidate",
         "SELECT COUNT(DISTINCT c.work_id) FROM candidate c JOIN work w USING(work_id)"
         " WHERE c.valid = 1 AND w.in_pool = 1"),
        ("select: chosen", "SELECT COUNT(*) FROM selection"),
        ("select: final songs", "SELECT COUNT(*) FROM work WHERE selected = 1"),
        ("select: validation-control extras", "SELECT COUNT(*) FROM work WHERE selected = 2"),
        ("analyze: songs ok", "SELECT COUNT(*) FROM song WHERE analysis_ok = 1"),
        ("analyze: songs failed", "SELECT COUNT(*) FROM song WHERE analysis_ok = 0"),
        ("influence: scored pairs", "SELECT COUNT(*) FROM pair_score"),
        ("influence: significant", "SELECT COUNT(*) FROM pair_score WHERE significant = 1"),
        ("influence: tree edges", "SELECT COUNT(*) FROM influence_edge WHERE kind = 'tree'"),
        ("themes: songs classified", "SELECT COUNT(*) FROM song_text"),
        ("themes: classified from lyrics", "SELECT COUNT(*) FROM song_text WHERE text_source = 'lyrics'"),
        ("themes: singers resolved", "SELECT COUNT(*) FROM singer WHERE gender <> 'unknown'"),
    ]
    for label, sql in rows:
        print(f"{label:45s} {_scalar(conn, sql):>8}")
    if not config.GRAPH_DB.exists():
        print(f"{'graph db':45s} {'missing':>8}")
        return 0
    graph = sqlite3.connect(f"file:{config.GRAPH_DB.as_posix()}?mode=ro", uri=True)
    for label, sql in (
        ("graph: songs", "SELECT COUNT(*) FROM nodes"),
        ("graph: influence edges", "SELECT COUNT(*) FROM influence_edges"),
        ("graph: roots", "SELECT COUNT(*) FROM song_node WHERE tree_parent_node IS NULL"),
        ("layout: positioned songs", "SELECT COUNT(*) FROM nodes WHERE position_x IS NOT NULL"),
        ("layout: runs", "SELECT COUNT(*) FROM layout_run"),
    ):
        print(f"{label:45s} {_scalar(graph, sql):>8}")
    graph.close()
    return 0
