"""``python -m musichistory canon``: fuse all-time song lists into a ranked pool of works.

Steps (docs/DESIGN.md §4):

1. **lists**: Billboard year-end 1946..last complete year, tsort top 5000, Rolling Stone
   2021/2004, Grammy Hall of Fame, Spotify most streamed, optional Acclaimed Music CSV.
2. **resolve** every row to a work: Wikipedia link -> QID, key match, guessed article
   titles, rationed search, else an ``R`` id; P2550 followed to the composition.
3. **fuse**: weighted reciprocal-rank fusion at work level -> ``canon_rank``.
4. **years**: Wikidata P577, list years, chart bounds (Billboard year-end, weekly Hot 100),
   MusicBrainz for the best-scoring works whose date is missing or suspect.
5. **pool**: top ``--pool-size`` plus a floor of 8 works per year 1950..last complete year.
6. **known_influence**: Wikidata P144/P2550 links and the validation controls.

Everything is cached under ``data/cache/canon``; a rerun (or a run resumed after an
interruption) repeats no request. The stage owns ``source_list``, ``list_entry``,
``work``, ``year_evidence`` and ``known_influence`` and replaces its rows on every run;
``work.selected`` (owned by select) is preserved, and a work that disappeared from the
lists is kept, unranked, while candidates still reference it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from .. import config, db
from .. import textnorm as tn
from . import acclaimed, billboard, grammy, hot100, rollingstone, spotify, tsort
from .fusion import Work, fuse, pool, rrf
from .known_influence import ControlIndex, WorkKeys, control_rows, load_controls, wikidata_rows
from .model import Entry, ListSource, Loaded
from .musicbrainz import MusicBrainz
from .net import Net
from .resolve import Resolution, Resolver, first_act
from .wikidata import Wikidata, pub_dates
from .years import Evidence, YearResult, decide

LISTS_CSV = config.ROOT / "lists" / "top_songs.csv"
CSV_COLUMNS = ["canon_rank", "work_id", "title", "artist", "original_artist", "work_year", "effective_year",
               "year_confidence", "rrf_score", "lists"]
FLOOR_FIRST_YEAR = 1950
FLOOR_PER_YEAR = 8


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--refresh", action="store_true",
                   help="re-download the list sources (new Wikipedia revisions, CSV files); API answers stay cached")
    p.add_argument("--skip-musicbrainz", action="store_true", help="do not query MusicBrainz for years")
    p.add_argument("--mb-limit", type=int, default=1200,
                   help="at most this many works (best score first) are checked on MusicBrainz (default 1200)")
    p.add_argument("--acclaimed", type=Path, default=None, help="optional user-exported Acclaimed Music CSV")
    p.add_argument("--pool-size", type=int, default=config.POOL_SIZE,
                   help=f"works handed to acquisition before the per-year floor (default {config.POOL_SIZE})")
    p.add_argument("--search-limit", type=int, default=400,
                   help="at most this many still-unresolved title/artist pairs are searched on Wikipedia, best "
                        "provisional score first (default 400; 0 disables search). Without MUSICHISTORY_CONTACT "
                        "Wikimedia allows 10 requests a minute, so each search costs ~6.5 s")
    p.add_argument("--pool-controls", action="store_true",
                   help="also pool every listed work named by a validation control (controls.json), so the "
                        "influence stage can test them; off by default (DESIGN.md pool rule)")
    p.add_argument("--last-year", type=int, default=None, help="last complete chart year (default: last year)")
    p.add_argument("--offline", action="store_true",
                   help="no network: list files must be cached; uncached API lookups are skipped")


# ---------------------------------------------------------------------------------- lists
def load_lists(net: Net, last_year: int, *, refresh: bool, acclaimed_csv: Path | None, log) -> list[Loaded]:
    out: list[Loaded] = []
    log("lists: Billboard year-end (Wikipedia)")
    out += billboard.load(net, last_year, refresh=refresh, log=log)
    log("lists: tsort, Rolling Stone, Grammy Hall of Fame, Spotify")
    out.append(tsort.load(net, refresh=refresh))
    out += rollingstone.load(net, refresh=refresh)
    out.append(grammy.load(net, refresh=refresh))
    out.append(spotify.load(net, refresh=refresh))
    if acclaimed_csv:
        out.append(acclaimed.load(acclaimed_csv))
    for l in out:
        if not l.entries:
            log(f"  WARNING: {l.source.list_id} has no rows")
    counts = Counter("billboard_ye" if l.source.list_id.startswith("billboard") else l.source.list_id for l in out
                     for _ in l.entries)
    log("  rows: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return out


# ---------------------------------------------------------------------------------- years
def _is_canonical(w: Work, e: Entry) -> bool:
    return any(x is e for x in w.canonical.entries)


def work_items(w: Work) -> set[str]:
    items = {e.qid for e in w.entries if e.qid}
    if w.qid:
        items.add(w.qid)
    return items


def performer_names(w: Work, res: Resolution) -> list[str]:
    """Every performer we know for a work: listed credits first, then Wikidata P175 labels."""
    names: list[str] = []
    for r in w.recordings:
        names.append(r.artist)
        names.extend(e.raw_artist for e in r.entries)
    for q in sorted(work_items(w)):
        names.extend(res.labels.get(p, "") for p in res.entities.get(q, {}).get("P175", []))
    out, seen = [], set()
    for n in names:
        k = tn.squash(tn.artist_key(n))
        if n and k and k not in seen:
            seen.add(k)
            out.append(n)
    return out


def base_evidence(w: Work, res: Resolution, idx: hot100.Hot100Index) -> tuple[list[Evidence], str | None]:
    """Wikidata, list and chart evidence for a work (MusicBrainz is added later)."""
    ev: list[Evidence] = []
    if w.qid:
        ent = res.entities.get(w.qid, {})
        dates = pub_dates(ent)
        for iso, prec in dates:
            ev.append(Evidence("wikidata:P577", iso, prec))
        if not dates and any(v.get("unknown") for v in ent.get("P577", [])):
            ev.append(Evidence("wikidata:P577", "unknown", 9))
    canon_items = {e.qid for e in w.canonical.entries if e.qid}
    for q in sorted(work_items(w) - {w.qid}):
        for iso, prec in pub_dates(res.entities.get(q, {})):
            ev.append(Evidence(f"wikidata:P577:{q}", iso, prec, recording=True, canonical=q in canon_items))
    seen: set[tuple[str, int, bool]] = set()
    for e in w.entries:
        if e.list_year:
            key = (e.list_id, e.list_year, _is_canonical(w, e))
            if key not in seen:
                seen.add(key)
                ev.append(Evidence(f"list:{e.list_id}", f"{e.list_year:04d}", 9, recording=True, canonical=key[2]))
    titles = list(dict.fromkeys(e.raw_title for e in w.entries))
    # lead acts only: a featured artist's own same-titled hit is another song
    run = idx.first_week(titles, list(dict.fromkeys(first_act(n) for n in performer_names(w, res))))
    first_week = run.first_week if run else None
    if run:
        ev.append(Evidence("hot100:first_week", run.first_week, 11))
    crun = idx.first_week(list(dict.fromkeys(e.raw_title for e in w.canonical.entries)), [first_act(w.canonical.artist)])
    if crun:
        ev.append(Evidence("hot100:canonical", crun.first_week, 11, recording=True, canonical=True))
    return ev, first_week


def _same_artist(name: str, canon: str) -> bool:
    """``name`` is the performer credited as ``canon`` (or one of its acts: "Cardi B" in a feat. credit)."""
    f, fc = tn.fold(name), tn.fold(canon)
    return tn.artist_matches(name, canon) >= 85 or bool(f and f" {f} " in f" {fc} ")


def p175_names(w: Work, res: Resolution) -> list[str]:
    return [n for n in (res.labels.get(p, "") for p in res.entities.get(w.qid or "", {}).get("P175", [])) if n]


def musicbrainz_evidence(w: Work, pre: YearResult, mb: MusicBrainz) -> list[tuple[Evidence, str]]:
    """(evidence, credited artist) from MusicBrainz for one work."""
    out: list[tuple[Evidence, str]] = []
    if pre.needs_musicbrainz == "traditional":
        got = mb.earliest_any(w.title, before=pre.work_year)
        if got:
            out.append((Evidence("musicbrainz:earliest", got[0], got[1]), got[2]))
        return out
    recs = [w.canonical] + sorted(w.recordings[1:], key=lambda r: min((e.list_year or 9999) for e in r.entries))
    asked: set[str] = set()
    for r in recs:
        artist = first_act(r.artist)
        k = tn.primary_artist(artist)
        if not artist or k in asked:
            continue
        asked.add(k)
        got = mb.earliest_by_artist(r.title, artist)
        if got:
            out.append((Evidence("musicbrainz:recording", got[0], got[1], recording=True,
                                 canonical=r is w.canonical), got[2]))
        if len(asked) >= 2:
            break
    return out


def original_artist(w: Work, yr: YearResult, mb_artists: list[tuple[Evidence, str]], res: Resolution) -> str | None:
    """First performer of the composition when it is known and differs from the famous one.

    Candidates are performers with a release within a year of ``work_year``: a MusicBrainz
    recording, or another listed recording that predates the famous one. Wikidata's first
    performer (P175) is used unverified only when the famous performer is not among the
    work's P175 performers at all ("Respect": Otis Redding); P175 order alone is not
    reliable enough otherwise (covers are often listed first).
    """
    canon = w.artist
    if yr.work_year is None:
        return None
    cands: list[tuple[int, str]] = []
    for ev, artist in mb_artists:
        if ev.year >= 1890 and abs(ev.year - yr.work_year) <= 1 and not _same_artist(artist, canon):
            cands.append((ev.year, artist))
    canon_min = min((e.list_year for e in w.canonical.entries if e.list_year), default=9999)
    for r in w.recordings[1:]:
        ys = [e.list_year for e in r.entries if e.list_year and e.list_year >= yr.work_year]
        if ys and min(ys) <= yr.work_year + 1 and min(ys) < canon_min and not _same_artist(r.artist, canon):
            cands.append((min(ys), r.artist))
    if cands:
        return min(cands, key=lambda c: c[0])[1]
    p175 = p175_names(w, res)
    if (p175 and yr.effective_year is not None and yr.work_year < yr.effective_year
            and not any(_same_artist(n, canon) for n in p175)):
        return p175[0]
    return None


def original_to_verify(w: Work, yr: YearResult, res: Resolution) -> str | None:
    """Wikidata's first performer, when it may be the original of a later famous cover."""
    p175 = p175_names(w, res)
    if (not p175 or yr.work_year is None or yr.effective_year is None or yr.work_year >= yr.effective_year
            or _same_artist(p175[0], w.artist)):
        return None
    return p175[0]


# ------------------------------------------------------------------------------------ db
def write_db(conn: sqlite3.Connection, sources: list[ListSource], entries: list[Entry], works: list[Work],
             rows: dict[str, dict], evidence: dict[str, list[Evidence]], influence: list[tuple[str, str, str, str]]) -> None:
    selected = {r[0]: r[1] for r in conn.execute("SELECT work_id, selected FROM work")}
    referenced = {r[0] for r in conn.execute("SELECT DISTINCT work_id FROM candidate")}
    with conn:
        conn.execute("DELETE FROM source_list")
        conn.execute("DELETE FROM list_entry")
        conn.execute("DELETE FROM year_evidence")
        conn.execute("DELETE FROM known_influence")
        conn.executemany(
            "INSERT INTO source_list(list_id, name, edition, url, revision, sha256, retrieved_at, license_note,"
            " weight, pseudo_rank) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(s.list_id, s.name, s.edition, s.url, s.revision, s.sha256, s.retrieved_at, s.license_note, s.weight,
              s.pseudo_rank) for s in sources])
        seen: set[tuple] = set()
        le = []
        for e in entries:
            key = (e.list_id, e.rank, e.raw_title, e.raw_artist)
            if key in seen:
                continue
            seen.add(key)
            le.append((e.list_id, e.rank, e.raw_title, e.raw_artist, e.list_year, e.wiki_link, e.weight_factor,
                       e.work_id, e.resolution_method, e.resolution_confidence))
        conn.executemany(
            "INSERT INTO list_entry(list_id, rank, raw_title, raw_artist, list_year, wiki_link, weight_factor,"
            " work_id, resolution_method, resolution_confidence) VALUES (?,?,?,?,?,?,?,?,?,?)", le)
        cols = ["work_id", "title", "canonical_artist", "original_artist", "search_artists", "work_date",
                "work_date_precision", "work_year", "effective_year", "year_confidence", "traditional",
                "wikidata_qid", "mb_work_id", "first_chart_week", "rrf_score", "canon_rank", "in_pool", "selected"]
        upd = ", ".join(f"{c} = excluded.{c}" for c in cols[1:-1])
        conn.executemany(
            f"INSERT INTO work({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
            f" ON CONFLICT(work_id) DO UPDATE SET {upd}",
            [tuple(rows[w.work_id][c] for c in cols[:-1]) + (selected.get(w.work_id, 0),) for w in works])
        current = {w.work_id for w in works}
        stale = [wid for wid in selected if wid not in current]
        for wid in stale:
            if wid in referenced:
                conn.execute("UPDATE work SET canon_rank = NULL, in_pool = 0, rrf_score = 0 WHERE work_id = ?", (wid,))
            else:
                conn.execute("DELETE FROM work WHERE work_id = ?", (wid,))
        conn.executemany(
            "INSERT INTO year_evidence(work_id, source, value, precision, accepted, reason) VALUES (?,?,?,?,?,?)",
            [(wid, e.source, e.value, e.precision, int(e.accepted), e.reason or None)
             for wid, evs in evidence.items() for e in evs])
        conn.executemany("INSERT OR REPLACE INTO known_influence(src_work_id, dst_work_id, kind, note) VALUES (?,?,?,?)",
                         influence)
    db.set_meta(conn, "canon_run_at", dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())


def write_csv(path: Path, works: list[Work], rows: dict[str, dict], unranked: frozenset[str] = frozenset()) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    pooled = [w for w in works if rows[w.work_id]["in_pool"]]
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, lineterminator="\n")
        wr.writerow(CSV_COLUMNS)
        for w in pooled:
            r = rows[w.work_id]
            wr.writerow([w.canon_rank, w.work_id, r["title"], r["canonical_artist"], r["original_artist"] or "",
                         r["work_year"] if r["work_year"] is not None else "",
                         r["effective_year"] if r["effective_year"] is not None else "",
                         r["year_confidence"], f"{w.score:.6f}", w.lists_summary(unranked)])
    tmp.replace(path)
    return len(pooled)


# ----------------------------------------------------------------------------------- run
def run(args: argparse.Namespace) -> int:
    t0 = time.time()

    def log(msg: str) -> None:
        print(f"[{time.time() - t0:7.0f}s] {msg}", flush=True)

    last_year = args.last_year or dt.date.today().year - 1
    net = Net(offline=args.offline)
    loaded = load_lists(net, last_year, refresh=args.refresh, acclaimed_csv=args.acclaimed, log=log)
    sources = {l.source.list_id: l.source for l in loaded}
    entries = [e for l in loaded for e in l.entries]

    def contribution(e: Entry) -> float:
        s = sources[e.list_id]
        return rrf(s.weight, s.pseudo_rank or e.rank, e.weight_factor)

    log(f"resolve: {len(entries)} rows")
    wd = Wikidata(net, log=log)
    resolver = Resolver(wd, search=args.search_limit != 0, log=log)
    res = resolver.run(entries, search_limit=args.search_limit or None, priority=contribution)
    log("  methods: " + ", ".join(f"{k} {v}" for k, v in sorted(res.stats.items())))

    works = fuse(entries, sources)
    log(f"fuse: {len(works)} works from {len(entries)} rows")

    idx, _ = hot100.load(net, refresh=args.refresh)
    label_top = [w for w in works[: args.pool_size + 2000] if w.qid]
    perf = {p for w in label_top for q in work_items(w) for p in res.entities.get(q, {}).get("P175", [])}
    todo = perf - set(res.labels)
    if todo:
        log(f"labels: {len(todo)} performers of the top works")
        res.labels.update(wd.labels(todo))

    log("years: Wikidata, lists, charts")
    base: dict[str, tuple[list[Evidence], str | None]] = {}
    pre: dict[str, YearResult] = {}
    for w in works:
        ev, fw = base_evidence(w, res, idx)
        base[w.work_id] = (ev, fw)
        # decide()'s single_performer / canonical_is_original rules stay off: Wikidata's P175
        # is too incomplete to know that a famous performer is the only one (see years.py).
        pre[w.work_id] = decide(ev, fw)
    mb_found: dict[str, list[tuple[Evidence, str]]] = {}
    need = [w for w in works if pre[w.work_id].needs_musicbrainz]
    log(f"  {len(need)} works need MusicBrainz ({Counter(pre[w.work_id].needs_musicbrainz for w in need)})")
    if not args.skip_musicbrainz and args.mb_limit > 0:
        mb = MusicBrainz(net)
        batch = need[: args.mb_limit]
        log(f"musicbrainz: checking {len(batch)} works (best score first, 1 request/s)")
        for i, w in enumerate(batch, 1):
            mb_found[w.work_id] = musicbrainz_evidence(w, pre[w.work_id], mb)
            if i % 100 == 0:
                log(f"    musicbrainz {i}/{len(batch)}")
    results: dict[str, YearResult] = {}

    def final(w: Work) -> None:
        ev, fw = base[w.work_id]
        results[w.work_id] = decide(ev + [e for e, _ in mb_found.get(w.work_id, [])], fw)

    for w in works:
        final(w)
    if not args.skip_musicbrainz and args.mb_limit > 0:
        # Covers: is Wikidata's first performer the original? One MusicBrainz lookup each,
        # for the works that can reach the pool.
        verify = [(w, a) for w in works[: args.pool_size + 500]
                  if (a := original_to_verify(w, results[w.work_id], res))
                  and original_artist(w, results[w.work_id], mb_found.get(w.work_id, []), res) is None]
        log(f"musicbrainz: verifying {len(verify)} possible original performers")
        for w, artist in verify:
            got = mb.earliest_by_artist(w.title, first_act(artist))
            if got:
                mb_found.setdefault(w.work_id, []).append(
                    (Evidence("musicbrainz:recording", got[0], got[1], recording=True), got[2]))
                final(w)

    in_pool, extra = pool(works, {w.work_id: results[w.work_id].work_year for w in works}, args.pool_size,
                          FLOOR_FIRST_YEAR, last_year, FLOOR_PER_YEAR)
    log(f"pool: {len(in_pool)} works ({args.pool_size} + {sum(extra.values())} for the per-year floor)")

    rows: dict[str, dict] = {}
    for w in works:
        yr = results[w.work_id]
        orig = original_artist(w, yr, mb_found.get(w.work_id, []), res)
        names = performer_names(w, res)
        if orig:
            names.insert(1, orig)
        search = []
        for n in names:
            for v in (n, first_act(n)):
                if v and all(tn.squash(tn.artist_key(v)) != tn.squash(tn.artist_key(s)) for s in search):
                    search.append(v)
        ent = res.entities.get(w.qid or "", {})
        rows[w.work_id] = dict(
            work_id=w.work_id, title=w.title, canonical_artist=w.artist, original_artist=orig,
            search_artists=json.dumps(search[:15], ensure_ascii=False, separators=(",", ":")),
            work_date=yr.work_date, work_date_precision=yr.work_date_precision, work_year=yr.work_year,
            effective_year=yr.effective_year, year_confidence=yr.year_confidence, traditional=int(yr.traditional),
            wikidata_qid=w.qid, mb_work_id=(ent.get("P435") or [None])[0],
            # Persist the chart week only as an ordering hint within the work's own year: a
            # reissue or cover that charted years later says nothing about the original's
            # place in its year (it stays an upper bound inside years.decide()).
            first_chart_week=(base[w.work_id][1] if yr.chart_week_ok and base[w.work_id][1]
                              and yr.work_year is not None
                              and int(str(base[w.work_id][1])[:4]) == yr.work_year else None),
            rrf_score=round(w.score, 8), canon_rank=w.canon_rank, in_pool=int(w.work_id in in_pool))

    log("known_influence: Wikidata P144/P2550 and validation controls")
    influence = wikidata_rows({w.work_id: work_items(w) for w in works if w.qid}, res.entities, res.work_of)
    keys = [WorkKeys(w.work_id, list(dict.fromkeys(e.raw_title for e in w.entries)),
                     json.loads(rows[w.work_id]["search_artists"]) + [rows[w.work_id]["original_artist"] or ""],
                     w.score, rows[w.work_id]["work_year"]) for w in works]
    ctl_rows, matches = control_rows(load_controls(), ControlIndex.build(keys))
    influence += ctl_rows
    found = sum(1 for m in matches if m.work_id)
    log(f"  {len(influence) - len(ctl_rows)} Wikidata links; controls: {found}/{len(matches)} songs resolved, "
        f"{len(ctl_rows)} control pairs")
    missing = sorted({f"{m.title} ({m.artist})" for m in matches if not m.work_id})
    if missing:
        log("  control songs not in any list: " + "; ".join(missing))
    outside = sorted({m.work_id for m in matches if m.work_id and m.work_id not in in_pool})
    if outside and args.pool_controls:
        for wid in outside:
            rows[wid]["in_pool"] = 1
            in_pool.add(wid)
        log(f"  --pool-controls: {len(outside)} control works added to the pool")
    elif outside:
        log(f"  {len(outside)} resolved control works are outside the pool (add them with --pool-controls)")

    conn = db.connect()
    write_db(conn, list(sources.values()), entries, works, rows,
             {w.work_id: results[w.work_id].evidence for w in works}, influence)
    n_csv = write_csv(LISTS_CSV, works, rows, frozenset(k for k, s in sources.items() if s.pseudo_rank))
    conf = Counter(rows[w]["year_confidence"] for w in in_pool)
    log(f"done: {len(works)} works, pool {len(in_pool)} (confidence {dict(conf)}), {n_csv} rows in "
        f"{LISTS_CSV.relative_to(config.ROOT)}; {net.requests} network requests")
    return 0
