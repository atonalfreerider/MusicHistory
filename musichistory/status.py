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
        ("analyze: songs ok", "SELECT COUNT(*) FROM song WHERE analysis_ok = 1"),
        ("analyze: songs failed", "SELECT COUNT(*) FROM song WHERE analysis_ok = 0"),
        ("influence: scored pairs", "SELECT COUNT(*) FROM pair_score"),
        ("influence: significant", "SELECT COUNT(*) FROM pair_score WHERE significant = 1"),
        ("influence: tree edges", "SELECT COUNT(*) FROM influence_edge WHERE kind = 'tree'"),
    ]
    for label, sql in rows:
        print(f"{label:45s} {_scalar(conn, sql):>8}")
    print(f"{'graph db':45s} {'present' if config.GRAPH_DB.exists() else 'missing':>8}")
    return 0
