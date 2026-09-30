"""Wikidata and Wikipedia lookups: article -> QID, search fallback, item claims, labels.

Everything goes through ``Net`` (sequential, ``maxlag=5`` on Wikidata) and is cached per
title / query / QID in ``api_cache.sqlite``, so a rerun costs no requests. Entities are
trimmed to the claims the canon stage reads before they are cached:

    P31 instance of          P577 publication date (+ precision, rank)
    P175 performer           P435 MusicBrainz work ID      P436 MusicBrainz release group ID
    P144 based on            P2550 recording or performance of
"""

from __future__ import annotations

from typing import Callable, Iterable

from .net import Net

# song, single, musical work/composition, musical work, audio track (docs/DESIGN.md), plus
# classes that song items were found typed with instead: the older "musical composition",
# music track with/without vocals, translated song, audio recording, single release,
# LGBT-related song.
SONG_TYPES = frozenset({
    "Q7366", "Q134556", "Q105543609", "Q2188189", "Q7302866",
    "Q207628", "Q55850593", "Q55850643", "Q63141557", "Q3302947", "Q108352496", "Q136489164",
})
ALBUM = "Q482994"
PROPS = ("P31", "P577", "P175", "P435", "P436", "P144", "P2550")
BATCH = 50


def _trim(entity: dict) -> dict:
    """Keep only the claims we use; drop deprecated statements."""
    out: dict = {"id": entity.get("id")}
    label = entity.get("labels", {}).get("en", {}).get("value")
    if label:
        out["label"] = label
    claims = entity.get("claims", {})
    for p in PROPS:
        vals = []
        for st in claims.get(p, []):
            if st.get("rank") == "deprecated":
                continue
            snak = st.get("mainsnak", {})
            if snak.get("snaktype") != "value":
                if p == "P577" and snak.get("snaktype") == "somevalue":
                    vals.append({"unknown": True})
                continue
            dv = snak.get("datavalue", {}).get("value")
            if p == "P577" and isinstance(dv, dict):
                vals.append({"time": dv.get("time"), "precision": dv.get("precision"), "rank": st.get("rank")})
            elif isinstance(dv, dict) and "id" in dv:
                vals.append(dv["id"])
            elif isinstance(dv, str):
                vals.append(dv)
        if vals:
            out[p] = vals
    return out


def is_song(entity: dict | None) -> bool:
    return bool(entity) and bool(SONG_TYPES & set(entity.get("P31", ())))


def pub_dates(entity: dict | None) -> list[tuple[str, int]]:
    """Non-deprecated P577 values as (ISO date at its precision, precision)."""
    out = []
    for v in (entity or {}).get("P577", []):
        t, prec = v.get("time"), v.get("precision")
        if not t or prec is None:
            continue
        sign = -1 if t.startswith("-") else 1
        body = t.lstrip("+-")
        year = int(body[:4]) * sign if body[:4].isdigit() else None
        if year is None:
            continue
        month, day = body[5:7], body[8:10]
        if prec >= 11 and day != "00" and month != "00":
            iso = f"{year:04d}-{month}-{day}"
        elif prec >= 10 and month != "00":
            iso, prec = f"{year:04d}-{month}", 10
        else:
            iso, prec = f"{year:04d}", min(prec, 9)
        out.append((iso, prec))
    return out


class Wikidata:
    def __init__(self, net: Net, log: Callable[[str], None] = print) -> None:
        self.net = net
        self.kv = net.kv
        self.log = log

    # -- Wikipedia title -> QID ---------------------------------------------------------
    def pageprops(self, titles: Iterable[str]) -> dict[str, dict]:
        """title -> {"page": resolved title, "qid": QID or None, "missing": bool}."""
        titles = [t for t in dict.fromkeys(titles) if t]
        out = self.kv.get_many("pageprops", titles)
        todo = [] if self.net.offline else [t for t in titles if t not in out]
        for i in range(0, len(todo), BATCH):
            batch = todo[i : i + BATCH]
            data = self.net.wikipedia({"action": "query", "titles": "|".join(batch), "prop": "pageprops",
                                       "ppprop": "wikibase_item", "redirects": "1"})
            q = data.get("query", {})
            norm = {n["from"]: n["to"] for n in q.get("normalized", [])}
            red = {r["from"]: r["to"] for r in q.get("redirects", [])}
            pages = {p["title"]: p for p in q.get("pages", [])}
            got = {}
            for t in batch:
                tt = norm.get(t, t)
                tt = red.get(tt, tt).split("#")[0]
                p = pages.get(tt, {})
                got[t] = {"page": tt, "qid": p.get("pageprops", {}).get("wikibase_item"),
                          "missing": bool(p.get("missing") or p.get("invalid"))}
            self.kv.put_many("pageprops", got)
            out.update(got)
            if (i // BATCH) % 20 == 19:
                self.log(f"    pageprops {i + len(batch)}/{len(todo)}")
        return out

    # -- search fallback -----------------------------------------------------------------
    def search(self, query: str) -> list[dict]:
        """MediaWiki full-text search: [{"page", "qid"}] in result order (cached)."""
        hit = self.kv.get("search", query)
        if hit is not None or self.net.offline:
            return hit or []
        data = self.net.wikipedia({"action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": "5",
                                   "gsrnamespace": "0", "prop": "pageprops", "ppprop": "wikibase_item"})
        pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
        res = [{"page": p["title"], "qid": p.get("pageprops", {}).get("wikibase_item")} for p in pages]
        self.kv.put("search", query, res)
        return res

    # -- entities --------------------------------------------------------------------------
    def entities(self, qids: Iterable[str]) -> dict[str, dict]:
        """QID -> trimmed entity ({} for missing items); follows Wikidata redirects."""
        qids = [q for q in dict.fromkeys(qids) if q and q.startswith("Q")]
        out = self.kv.get_many("wd", qids)
        todo = [] if self.net.offline else [q for q in qids if q not in out]
        for i in range(0, len(todo), BATCH):
            batch = todo[i : i + BATCH]
            data = self.net.wikidata({"action": "wbgetentities", "ids": "|".join(batch),
                                      "props": "claims|labels", "languages": "en"})
            got: dict[str, dict] = {}
            ents = data.get("entities", {})
            for q in batch:
                e = ents.get(q)
                if e is None:  # redirected: the answer is keyed by the target id
                    e = next((v for v in ents.values() if (v.get("redirects") or {}).get("from") == q), None)
                if e is None or "missing" in e:
                    got[q] = {}
                else:
                    got[q] = _trim(e)
                    if got[q].get("id") != q:
                        got[q]["redirect_to"] = got[q]["id"]
            self.kv.put_many("wd", got)
            out.update(got)
            if (i // BATCH) % 20 == 19:
                self.log(f"    wikidata entities {i + len(batch)}/{len(todo)}")
        return out

    def labels(self, qids: Iterable[str]) -> dict[str, str]:
        """QID -> English label ('' when none)."""
        qids = [q for q in dict.fromkeys(qids) if q and q.startswith("Q")]
        out = self.kv.get_many("wd_label", qids)
        todo = [] if self.net.offline else [q for q in qids if q not in out]
        for i in range(0, len(todo), BATCH):
            batch = todo[i : i + BATCH]
            data = self.net.wikidata({"action": "wbgetentities", "ids": "|".join(batch), "props": "labels",
                                      "languages": "en", "languagefallback": "1"})
            ents = data.get("entities", {})
            got = {q: (ents.get(q, {}).get("labels", {}).get("en", {}) or {}).get("value", "") for q in batch}
            self.kv.put_many("wd_label", got)
            out.update(got)
        return out

    def follow_p2550(self, qids: Iterable[str], ents: dict[str, dict], depth: int = 3) -> dict[str, str]:
        """QID -> the composition it is a recording of (itself when it has no song-typed P2550)."""
        qids = list(dict.fromkeys(qids))
        target: dict[str, str] = {q: q for q in qids}
        frontier = [q for q in qids if ents.get(q, {}).get("P2550")]
        for _ in range(depth):
            if not frontier:
                break
            wanted = {t for q in frontier for t in ents[q].get("P2550", [])}
            ents.update(self.entities(wanted - set(ents)))
            nxt = []
            for q in frontier:
                cands = [ents[t].get("redirect_to", t) for t in ents[q].get("P2550", []) if is_song(ents.get(t))]
                cands = [t for t in cands if t != q]
                if cands:
                    target[q] = cands[0]
                    if ents.get(cands[0], {}).get("P2550") and cands[0] not in target:
                        nxt.append(cands[0])
            frontier = nxt
        for q in qids:  # collapse chains
            seen = {q}
            while target.get(target[q], target[q]) != target[q] and target[q] not in seen:
                seen.add(target[q])
                target[q] = target.get(target[q], target[q])
        return target
