"""``python -m musichistory photos [--all] [--artist QID ...] [--refresh]``: freely licensed
photographs of the singers and groups (DESIGN §15).

The artists are the lead acts the ``themes`` stage resolved (table ``singer``: ``artist_qid``,
duets as ``Q1;Q2``) of the songs on the featured paths (``data/audio/renders/paths.json``),
or of every selected song with ``--all``. For each artist (``choose``): the Wikidata image
(P18) first, else Commons files that depict the artist or sit in the artist's Commons
category, ranked by era and kind of shot; only free licences pass (``licence``). The 720 px
thumbnail is saved as ``data/images/artists/artist-<QID>.jpg`` (re-encoded with ffmpeg when
Commons serves a wider rendition or a PNG) and catalogued in ``data/images/artists.json``
(``catalogue``); ``data/images/artists_report.json`` lists every artist without a photo and
why. Requests are sequential, polite and cached under ``data/cache/photos/`` (``commons``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .. import config
from . import catalogue, choose
from .choose import Artist, Candidate
from .commons import THUMB_WIDTH, Wikimedia, clean_url

Log = Callable[[str], None]


def images_dir() -> Path:
    return config.DATA / "images"


def catalogue_path() -> Path:
    return images_dir() / "artists.json"


def report_path() -> Path:
    return images_dir() / "artists_report.json"


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--all", action="store_true", help="every selected song's lead act, not only the featured paths'")
    p.add_argument("--artist", action="append", metavar="QID", help="only this artist QID (repeatable)")
    p.add_argument("--refresh", action="store_true", help="ask Wikidata/Commons again instead of the cache")


def _log(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- inputs
def featured_works(paths_json: Path | None = None) -> dict[str, dict]:
    """work_id -> {"year", "credit"} of every song on the featured paths."""
    path = paths_json or config.DATA / "audio" / "renders" / "paths.json"
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for p in doc.get("paths", []):
        for s in p.get("steps", []):
            if s.get("work_id"):
                out.setdefault(s["work_id"], {"year": s.get("year"), "credit": s.get("artist") or ""})
    return out


def all_works(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["work_id"]: {"year": r["year"], "credit": r["canonical_artist"] or ""} for r in conn.execute(
        "SELECT work_id, canonical_artist, COALESCE(effective_year, work_year) AS year FROM work"
        " WHERE selected >= 1 ORDER BY canon_rank, work_id")}


def artists_of(conn: sqlite3.Connection, works: dict[str, dict]) -> tuple[dict[str, Artist], list[dict]]:
    """Artists (QID -> Artist, first-seen order) of ``works`` from the ``singer`` table, and
    the works whose lead act is unresolved."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='singer'")}
    rows = {}
    if have:
        ids = list(works)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(f"SELECT work_id, artist_qid, artist_label FROM singer"
                                  f" WHERE work_id IN ({','.join('?' * len(chunk))})", chunk):
                rows[r["work_id"]] = r
    artists: dict[str, Artist] = {}
    unresolved = []
    for wid, w in works.items():
        r = rows.get(wid)
        qids = [q.strip() for q in ((r["artist_qid"] if r else None) or "").split(";") if q.strip()]
        if not qids:
            unresolved.append({"work_id": wid, "credit": w.get("credit", "")})
            continue
        labels = (r["artist_label"] or "").split(" & ")
        labels = labels if len(labels) == len(qids) else [""] * len(qids)
        for q, lab in zip(qids, labels):
            a = artists.setdefault(q, Artist(q, lab))
            if wid not in a.work_ids:
                a.work_ids.append(wid)
            if isinstance(w.get("year"), int) and w["year"] not in a.years:
                a.years.append(w["year"])
            if w.get("credit") and w["credit"] not in a.names:
                a.names.append(w["credit"])
    return artists, unresolved


# --------------------------------------------------------------------------- choosing
def find_photo(wm: Wikimedia, a: Artist, ent: dict, p18_info: dict[str, dict], latest: int,
               failed: dict[str, str] | None = None) -> tuple[Candidate | None, list[Candidate]]:
    """The best free photo of an artist, and every candidate considered. ``failed``: file
    titles that could not be downloaded (title -> reason); with any, the search runs even
    when Wikidata's image would otherwise have been enough."""
    failed = failed or {}
    subject = ent.get("label") or a.label or a.qid
    considered: list[Candidate] = []
    names = [subject, a.label] + a.names + ([re.sub(r"\s*\(.*\)$", "", ent["P373"])] if ent.get("P373") else [])
    names = [n for n in dict.fromkeys(names) if n]

    def add(c: Candidate, preferred: bool = False) -> None:
        choose.evaluate(c, a.years, latest, preferred=preferred, names=names)
        if c.title in failed:
            c.reason = failed[c.title]
        considered.append(c)

    for img in ent.get("P18", []):
        f = p18_info.get(img["file"])
        if f is None:
            considered.append(Candidate("File:" + img["file"], "p18", {}, subject, a.qid, reason="file missing on Commons"))
            continue
        c = Candidate(f["title"], "p18", f["info"], subject, a.qid)
        add(c, img.get("preferred", False))
        if c.ok and c.year is None and img.get("year"):
            c.year = img["year"]  # P585 "point in time" on the P18 statement
    top = choose.best(considered)
    if not failed and not choose.needs_search(top, a.years):
        return top, considered
    seen = {c.title for c in considered}
    for f in wm.depicts(a.qid):
        if f["title"] not in seen:
            seen.add(f["title"])
            add(Candidate(f["title"], "depicts", f["info"], subject, a.qid))
    if ent.get("P373"):
        titles = [t for t in wm.category_files(ent["P373"]) if t not in seen]
        short = choose.category_shortlist(titles, names, a.years)
        info = wm.imageinfo(short) if short else {}
        for t in short:
            if t in info and info[t]["title"] not in seen:
                seen.add(info[t]["title"])
                add(Candidate(info[t]["title"], "category", info[t]["info"], subject, a.qid))
    top = choose.best(considered)
    if top is None and "Q5" not in ent.get("P31", []) and ent.get("P527"):
        members = wm.entities(ent["P527"])
        for m in choose.leaders(ent, members):
            titles = [img["file"] for img in m.get("P18", [])]
            info = wm.imageinfo(titles) if titles else {}
            for img in m.get("P18", []):
                f = info.get(img["file"])
                if f and f["title"] not in seen:
                    seen.add(f["title"])
                    add(Candidate(f["title"], "leader", f["info"], m.get("label") or m["id"], m["id"]),
                        img.get("preferred", False))
        top = choose.best(considered)
    return top, considered


# --------------------------------------------------------------------------- files
def to_jpeg(data: bytes, max_width: int = THUMB_WIDTH, ffmpeg: str | None = None) -> bytes:
    """``data`` as a JPEG at most ``max_width`` px wide (unchanged when it already is one);
    re-encoded with ffmpeg otherwise (Lanczos downscale, quality 3, metadata dropped)."""
    size = catalogue.jpeg_size(data)
    if size and size[0] <= max_width:
        return data
    if ffmpeg is None:
        from ..paths.ffmpeg import find_ffmpeg

        ffmpeg = find_ffmpeg()
    tmpdir = config.CACHE / "photos" / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmpdir) as d:
        src, dst = Path(d) / "in.img", Path(d) / "out.jpg"
        src.write_bytes(data)
        cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src),
               "-vf", f"scale='min({max_width},iw)':-2:flags=lanczos,format=yuvj420p",
               "-frames:v", "1", "-q:v", "3", "-map_metadata", "-1", str(dst)]
        subprocess.run(cmd, check=True, capture_output=True)
        return dst.read_bytes()


def caption_for(c: Candidate) -> str:
    """A short neutral caption: who, and when when known ("The Police in 1979")."""
    return f"{c.subject} in {c.year}" if c.year else c.subject


def entry_for(a: Artist, c: Candidate, size: tuple[int, int]) -> dict:
    info = c.info
    page = clean_url(info.get("descriptionurl")) or "https://commons.wikimedia.org/wiki/" + c.title.replace(" ", "_")
    return {
        "file": catalogue.file_for(a.qid),
        "subject": c.subject,
        "artist_qid": a.qid,
        "work_ids": list(a.work_ids),
        "commons_page": page,
        "commons_file": c.title,
        "author": c.lic.author,
        "license": c.lic.short,
        "license_url": c.lic.url,
        "caption": caption_for(c),
        "width": size[0],
        "height": size[1],
        "year": c.year,
        "source": c.source,
    }


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- driver
def collect(artists: dict[str, Artist], wm: Wikimedia, out_dir: Path, *, to_jpeg_fn=to_jpeg,
            latest: int | None = None, log: Log = _log) -> tuple[dict[str, dict], list[dict]]:
    """Find, download and catalogue a photo per artist: (catalogue entries, misses)."""
    latest = latest or dt.date.today().year
    ents = wm.entities(artists)
    p18_titles = [img["file"] for e in ents.values() for img in e.get("P18", [])]
    p18_info = wm.imageinfo(p18_titles) if p18_titles else {}
    entries: dict[str, dict] = {}
    misses: list[dict] = []
    for n, a in enumerate(artists.values(), 1):
        ent = ents.get(a.qid) or {}
        if ent.get("label"):
            a.label = ent["label"]
        failed: dict[str, str] = {}
        top, considered = find_photo(wm, a, ent, p18_info, latest)
        chosen = None
        while top is not None:
            try:
                url = clean_url(top.info.get("thumburl") or top.info.get("url"))
                data = to_jpeg_fn(wm.download(url))
                size = catalogue.jpeg_size(data)
                if size is None:
                    raise ValueError("not a JPEG after conversion")
                _write_bytes(out_dir / catalogue.file_for(a.qid), data)
                chosen = top
                entries[catalogue.image_id(a.qid)] = entry_for(a, top, size)
                break
            except Exception as exc:  # a failed download or conversion: try the next candidate
                top.reason = failed[top.title] = f"download failed: {exc}"
                log(f"    {top.title}: {top.reason}")
                top = choose.best(considered)
                if top is None and len(failed) <= 3:  # widen the search once more
                    top, considered = find_photo(wm, a, ent, p18_info, latest, failed)
        if chosen is not None:
            log(f"  [{n}/{len(artists)}] {a.label}: {chosen.source} {chosen.title!r} ({chosen.lic.short},"
                f" {chosen.year or 'undated'})")
        else:
            reasons = [{"title": c.title, "source": c.source, "reason": c.reason} for c in considered][:15]
            why = "no image on Wikidata or Commons" if not considered else "no free, usable photo"
            misses.append({"artist_qid": a.qid, "label": a.label, "work_ids": a.work_ids, "reason": why,
                           "rejected": reasons})
            log(f"  [{n}/{len(artists)}] {a.label}: no photo ({why}; {len(considered)} candidates)")
    return entries, misses


def run(args: argparse.Namespace) -> int:
    from .. import db

    t0 = time.monotonic()
    conn = db.connect()
    works = all_works(conn) if args.all else featured_works()
    artists, unresolved = artists_of(conn, works)
    if args.artist:
        artists = {q: a for q, a in artists.items() if q in set(args.artist)}
    scope = "all" if args.all else "featured"
    _log(f"photos: {len(works)} songs ({scope}), {len(artists)} artists, {len(unresolved)} songs without a resolved act")
    if not artists:
        _log("photos: nothing to do (run the themes stage first to resolve the lead acts)")
        return 1 if not works else 0
    wm = Wikimedia(refresh=args.refresh, log=_log)
    root = images_dir()
    entries, misses = collect(artists, wm, root)
    path = catalogue_path()
    run_keys = {catalogue.image_id(q) for q in artists}
    # an artist of this run without a photo now loses its old one; other artists are kept
    doc = catalogue.merge(catalogue.load(path), entries,
                          keep=lambda k, v: k in entries or (k not in run_keys and (root / v.get("file", "")).exists()))
    errs = catalogue.validate(doc, root)
    if errs:
        for e in errs[:20]:
            _log(f"  catalogue error: {e}")
        return 1
    catalogue.write(path, doc)
    for key in sorted(run_keys - set(doc["images"])):  # a stale photo of an artist that has none now
        (root / catalogue.file_for(key.removeprefix("artist-"))).unlink(missing_ok=True)
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": scope,
        "artists": len(artists),
        "with_photo": len(entries),
        "sources": {s: sum(1 for e in entries.values() if e["source"] == s) for s in sorted(catalogue.SOURCES)},
        "missing": misses,
        "unresolved_songs": unresolved,
        "requests": dict(wm.requests),
        "cache_answers": dict(wm.cached),
    }
    catalogue.write(report_path(), report)
    _log(f"photos: {len(entries)}/{len(artists)} artists have a free photo; catalogue {path} ({len(doc['images'])} images)")
    for m in misses:
        _log(f"  missing: {m['label']} ({m['artist_qid']}): {m['reason']}")
    _log(f"photos: requests {dict(wm.requests)}, cache answers {dict(wm.cached)}; {time.monotonic() - t0:.0f} s")
    return 0
