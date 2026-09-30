"""Weekly Billboard Hot 100 (1958-08-04 onward), used for dates only.

Ranking with weekly data would be ~98 % post-1990 (modern chart rules keep songs on the
chart far longer), so it only supplies ``work.first_chart_week``: the earliest week any
known recording of a work entered the chart, an upper bound on its release date.
Data: utdata/rwd-billboard-data (MIT), pinned by commit.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass

from .. import textnorm as tn
from .net import Net

COMMIT = "8ad259131a515ffaa55218c6959c03b9e301f0c4"
URL = f"https://raw.githubusercontent.com/utdata/rwd-billboard-data/{COMMIT}/data-out/hot-100-current.csv"
FILE = "hot-100-current.csv"


@dataclass
class ChartRun:
    title: str
    performer: str
    first_week: str   # ISO date


def chart_artist_ok(chart_performer: str, listed: str) -> bool:
    """The chart credit's lead act is the listed performer (or is named inside its credit).

    Stricter than ``textnorm.artist_matches``, whose token containment lets "Drake" match
    "Pete Drake" and so date Drake's 2009 "Forever" by Pete Drake's 1964 "Forever".
    """
    lead = tn.primary_artist(chart_performer)
    if not lead:
        return False
    if tn.squash(lead) in (tn.squash(tn.primary_artist(listed)), tn.squash(tn.artist_key(listed))):
        return True
    lead_tokens = {tn.drop_g(t) for t in lead.split() if t != "and"}
    listed_tokens = {tn.drop_g(t) for t in tn.fold(listed).split()} | {
        tn.drop_g(t) for t in tn.artist_key(listed).split()}
    return bool(lead_tokens) and lead_tokens <= listed_tokens


class Hot100Index:
    """Earliest chart week per (title, performer), looked up by normalized title."""

    def __init__(self, runs: list[ChartRun]) -> None:
        self.by_title: dict[str, list[ChartRun]] = defaultdict(list)
        for run in runs:
            for k in {tn.squash(tn.title_key(run.title)), tn.squash(tn.title_core(run.title))}:
                if k:
                    self.by_title[k].append(run)
        self.n_runs = len(runs)

    def first_week(self, titles: list[str], artists: list[str]) -> ChartRun | None:
        """Earliest run whose title matches one of ``titles`` and performer one of ``artists``."""
        best: ChartRun | None = None
        seen: set[int] = set()
        for t in titles:
            for k in {tn.squash(tn.title_key(t)), tn.squash(tn.title_core(t))}:
                for run in self.by_title.get(k, ()):
                    if id(run) in seen:
                        continue
                    seen.add(id(run))
                    if best is not None and run.first_week >= best.first_week:
                        continue
                    if any(chart_artist_ok(run.performer, a) for a in artists if a):
                        best = run
        return best


def parse(text: str) -> list[ChartRun]:
    first: dict[tuple[str, str], str] = {}
    for r in csv.DictReader(io.StringIO(text)):
        key = (r["title"], r["performer"])
        week = r["chart_week"]
        if key not in first or week < first[key]:
            first[key] = week
    return [ChartRun(t, p, w) for (t, p), w in first.items()]


def load(net: Net, *, refresh: bool = False) -> tuple[Hot100Index, dict]:
    data, meta = net.fetch_file(FILE, URL, revision=COMMIT, refresh=refresh)
    return Hot100Index(parse(data.decode("utf-8-sig", errors="replace"))), meta
