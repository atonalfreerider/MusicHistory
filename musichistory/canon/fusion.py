"""Work-level weighted reciprocal-rank fusion and the acquisition pool (pure functions).

``score(work) = sum over every listed recording of the work, over every list l of
W_l * f / (K + rank_l)`` with K = 60 (Cormack, Clarke & Buettcher 2009), f = 0.5 for the B
side of a double A-side, and an unranked list (Grammy Hall of Fame) at its pseudo rank.
Billboard counts as one list per year with weight 1, so every chart year contributes the
same total and no era dominates by list length.

The pool is the top ``pool_size`` works plus, for every year from 1950 to the last
complete year with fewer than ``per_year`` pooled works, the best-scoring remaining works
of that year.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .. import textnorm as tn
from .model import Entry, ListSource

K = 60
# Display spellings: prefer sources with edited titles/credits over all-caps or CSV ones.
TITLE_PREFERENCE = ("billboard", "grammy", "spotify", "rs500_2021", "rs500_2004", "tsort", "acclaimed")


def rrf(weight: float, rank: int, factor: float = 1.0, k: int = K) -> float:
    return weight * factor / (k + rank)


def recording_key(title: str, artist: str) -> tuple[str, str]:
    return tn.squash(tn.title_core(title)), tn.primary_artist(artist)


def _pref(list_id: str) -> int:
    for i, p in enumerate(TITLE_PREFERENCE):
        if list_id.startswith(p):
            return i
    return len(TITLE_PREFERENCE)


@dataclass
class Recording:
    key: tuple[str, str]
    entries: list[Entry] = field(default_factory=list)
    score: float = 0.0

    def _display(self) -> Entry:
        return min(self.entries, key=lambda e: (_pref(e.list_id), e.list_id, e.rank))

    @property
    def title(self) -> str:
        return self._display().raw_title

    @property
    def artist(self) -> str:
        return self._display().raw_artist


@dataclass
class Work:
    work_id: str
    qid: str | None
    entries: list[Entry] = field(default_factory=list)
    recordings: list[Recording] = field(default_factory=list)   # best first
    score: float = 0.0
    canon_rank: int = 0

    @property
    def canonical(self) -> Recording:
        return self.recordings[0]

    @property
    def title(self) -> str:
        return self.canonical.title

    @property
    def artist(self) -> str:
        return self.canonical.artist

    def lists_summary(self, unranked: frozenset[str] = frozenset()) -> str:
        """'billboard_ye_1993#1;grammy_hof;tsort_5000#3' (best rank per list; none for unranked lists)."""
        best: dict[str, int] = {}
        for e in self.entries:
            best[e.list_id] = min(best.get(e.list_id, 10**9), e.rank)
        return ";".join(lid if lid in unranked else f"{lid}#{r}"
                        for lid, r in sorted(best.items(), key=lambda kv: (_pref(kv[0]), kv[0])))


def fuse(entries: list[Entry], sources: dict[str, ListSource], k: int = K) -> list[Work]:
    """Group resolved entries into works, score them, and return works ranked 1..N."""
    by_work: dict[str, Work] = {}
    for e in entries:
        if not e.work_id:
            continue
        src = sources[e.list_id]
        rank = src.pseudo_rank if src.pseudo_rank else e.rank
        w = by_work.setdefault(e.work_id, Work(e.work_id, e.work_id if e.work_id.startswith("Q") else None))
        w.entries.append(e)
        w.score += rrf(src.weight, rank, e.weight_factor, k)
    for w in by_work.values():
        recs: dict[tuple[str, str], Recording] = {}
        for e in w.entries:
            src = sources[e.list_id]
            rank = src.pseudo_rank if src.pseudo_rank else e.rank
            r = recs.setdefault(recording_key(e.raw_title, e.raw_artist), Recording(recording_key(e.raw_title, e.raw_artist)))
            r.entries.append(e)
            r.score += rrf(src.weight, rank, e.weight_factor, k)
        w.recordings = sorted(recs.values(), key=lambda r: (-r.score, r.key))
    ranked = sorted(by_work.values(), key=lambda w: (-w.score, -len({e.list_id for e in w.entries}), w.work_id))
    for i, w in enumerate(ranked, 1):
        w.canon_rank = i
    return ranked


def pool(ranked: list[Work], years: dict[str, int | None], pool_size: int,
         first_year: int = 1950, last_year: int = 2025, per_year: int = 8) -> tuple[set[str], dict[int, int]]:
    """(work ids in the pool, {year: extra works added for the floor})."""
    chosen = {w.work_id for w in ranked[:pool_size]}
    count = Counter(years.get(w) for w in chosen)
    extra: dict[int, int] = defaultdict(int)
    for w in ranked[pool_size:]:
        y = years.get(w.work_id)
        if y is not None and first_year <= y <= last_year and count[y] < per_year:
            chosen.add(w.work_id)
            count[y] += 1
            extra[y] += 1
    return chosen, dict(extra)
