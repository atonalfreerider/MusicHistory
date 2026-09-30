"""``python -m musichistory fetch``: collect candidate MIDI files for every pooled work.

Order (DESIGN.md §5): Hooktheory matching (reference only), Lakh (offline, one tarball
pass per batch), midicollection (offline index + downloads), freemidi (works with fewer
than ``--freemidi-below`` valid candidates), midiworld (works with fewer than
``--midiworld-below``). Every step is resumable: works already searched by a source and
files already tried are skipped unless ``--refresh``. A per-run summary is printed and
written to ``data/reports/fetch_<timestamp>.json``.

After a sanitizer change: ``--resanitize`` re-extracts and re-sanitizes every stored Lakh
candidate, ``--redownload-web`` downloads every stored web candidate again by its stored
URL (raw web files are never kept) and re-sanitizes it; both update rows in place, so
``candidate_id`` stays stable. With ``--redownload-web`` the web sources are not searched.
"""

from __future__ import annotations

import argparse
import json
import time

from .. import config, db
from ..http import PoliteClient
from . import base, freemidi, hooktheory, lakh, midicollection, midiworld

ALL_SOURCES = ("lakh", "midicollection", "freemidi", "midiworld")


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sources", default=",".join(ALL_SOURCES),
                   help="comma-separated subset of: " + ", ".join(ALL_SOURCES))
    p.add_argument("--work", action="append", help="only this work_id (repeatable); ignores in_pool")
    p.add_argument("--limit", type=int, help="only the first N pooled works by canon_rank")
    p.add_argument("--max-per-source", type=int, default=base.MAX_PER_SOURCE)
    p.add_argument("--midicollection-below", type=int, default=4,
                   help="midicollection only for works with fewer valid candidates (0 = every work)")
    p.add_argument("--freemidi-below", type=int, default=2)
    p.add_argument("--midiworld-below", type=int, default=1)
    p.add_argument("--refresh", action="store_true", help="search works again that were already searched")
    p.add_argument("--rebuild-index", action="store_true", help="rebuild the Lakh and midicollection indexes")
    p.add_argument("--resanitize", action="store_true",
                   help="re-extract and re-sanitize every Lakh candidate (after sanitizer changes)")
    p.add_argument("--redownload-web", action="store_true",
                   help="download every stored web candidate again by its URL and re-sanitize it in place"
                        " (after sanitizer changes); no web searches in this run")
    p.add_argument("--offline", action="store_true", help="no network: Lakh and Hooktheory from the cache only")
    p.add_argument("--no-hooktheory", action="store_true")


def client(conn) -> PoliteClient:
    return PoliteClient(
        min_interval={"midicollection.com": midicollection.INTERVAL, "freemidi.org": freemidi.INTERVAL,
                      "www.midiworld.com": midiworld.INTERVAL},
        default_interval=3.0, timeout=30.0, cache_dir=config.CACHE / "http", db=conn)


def run(args: argparse.Namespace) -> int:
    conn = db.connect()
    base.ensure_schema(conn)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = set(sources) - set(ALL_SOURCES)
    if unknown:
        print(f"unknown sources: {sorted(unknown)}")
        return 2
    works = base.load_works(conn, ids=args.work, limit=args.limit)
    if not works:
        print("fetch: no works (run canon first, or check --work ids)")
        return 1
    print(f"fetch: {len(works)} works; sources {', '.join(sources)}", flush=True)
    t_start = time.monotonic()
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "works": len(works), "sources": {}}

    if not args.no_hooktheory:
        t0 = time.monotonic()
        try:
            n_w, n_s = hooktheory.match_works(conn, works, offline=args.offline)
            report["hooktheory"] = {"works": n_w, "sections": n_s, "seconds": round(time.monotonic() - t0, 1)}
            print(f"hooktheory: {n_w}/{len(works)} works, {n_s} sections", flush=True)
        except Exception as exc:
            report["hooktheory"] = {"error": str(exc)}
            print(f"hooktheory: skipped ({exc})", flush=True)

    stats: list[base.SourceStats] = []
    if "lakh" in sources:
        stats += lakh.fetch(conn, works, max_per_source=args.max_per_source, rebuild=args.rebuild_index,
                            offline=args.offline, resanitize=args.resanitize)
        _print(stats[-2:])
    web = [s for s in sources if s != "lakh"]
    if web and args.offline:
        print("fetch: --offline, skipping " + ", ".join(web))
        web = []
    if web and args.redownload_web:
        cl = client(conn)
        adapters = {"midicollection": midicollection, "freemidi": freemidi, "midiworld": midiworld}
        for s in web:
            print(f"{s}: re-downloading stored candidates", flush=True)
            stats.append(adapters[s].redownload(conn, works, cl))
            _print(stats[-1:])
    elif web:
        cl = client(conn)
        if "midicollection" in web:
            below = args.midicollection_below if args.midicollection_below > 0 else None
            stats.append(midicollection.fetch(conn, works, cl, max_per_source=args.max_per_source,
                                              refresh=args.refresh, refresh_index=args.rebuild_index, below=below))
            _print(stats[-1:])
        if "freemidi" in web:
            stats.append(freemidi.fetch(conn, works, cl, below=args.freemidi_below, refresh=args.refresh))
            _print(stats[-1:])
        if "midiworld" in web:
            stats.append(midiworld.fetch(conn, works, cl, below=args.midiworld_below, refresh=args.refresh))
            _print(stats[-1:])

    ids = [w.work_id for w in works]
    counts = base.valid_counts(conn)
    covered = sum(1 for i in ids if counts.get(i, 0) > 0)
    report["sources"] = {s.source: s.as_dict() for s in stats}
    report["works_with_valid"] = covered
    report["works_with_2_valid"] = sum(1 for i in ids if counts.get(i, 0) >= 2)
    report["seconds"] = round(time.monotonic() - t_start, 1)
    out = config.DATA / "reports" / f"fetch_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"fetch: {covered}/{len(works)} works have a valid candidate "
          f"({report['works_with_2_valid']} have >= 2); {report['seconds']} s; report {out}")
    return 0


def _print(stats: list[base.SourceStats]) -> None:
    for s in stats:
        print(f"  {s.source:15s} works={s.works} matched={s.matched_works} hits={s.hits} new/valid={s.valid}"
              f" dup={s.duplicate} invalid={s.invalid} failed={s.failed} rejected={s.rejected}"
              f"{f' changed={s.changed}' if s.changed else ''}"
              f" {s.seconds:.1f}s {s.invalid_reasons or ''}{s.failures or ''}", flush=True)
