"""Cached, polite access to Wikidata and Wikimedia Commons for the photos stage.

Every API answer is cached in ``data/cache/photos/api.sqlite`` (keyed by the request), every
downloaded thumbnail under ``data/cache/photos/http/``. Requests are sequential; the
Wikidata and Commons APIs share one rate budget (canon's Wikimedia interval: 6.5 s without
``MUSICHISTORY_CONTACT``, 0.7 s with it), carry ``maxlag=5`` and back off on ``maxlag``
errors and 429/5xx. The User-Agent is ``musichistory.config.user_agent()``.

``http`` is anything with ``get(url, params=..., robots=..., use_cache=...)`` returning an
object with ``status``, ``headers``, ``content`` and ``json()`` (``musichistory.http.PoliteClient``
by default; tests pass a fake).
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from .. import config
from ..canon.net import KVCache, WIKIDATA_API, WIKIMEDIA_INTERVAL

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PHOTOS_CACHE = config.CACHE / "photos"
THUMB_WIDTH = 720
BATCH = 50
EXT_FIELDS = ("LicenseShortName|License|LicenseUrl|UsageTerms|AttributionRequired|NonFree|Copyrighted|"
              "Restrictions|Artist|Credit|DateTimeOriginal|ImageDescription|Categories|ObjectName")

Log = Callable[[str], None]


class ApiError(RuntimeError):
    pass


def clean_url(url: str | None) -> str | None:
    """``url`` without the ``utm_*`` tracking query Commons appends."""
    if not url:
        return url
    parts = urlsplit(url)
    query = "&".join(q for q in parts.query.split("&") if q and not q.startswith("utm_"))
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, query, ""))


def _value(snak: dict) -> Any:
    if snak.get("snaktype") != "value":
        return None
    return snak.get("datavalue", {}).get("value")


def _year(snaks: list[dict]) -> int | None:
    for s in snaks:
        v = _value(s)
        m = re.match(r"^[+-]?(\d{1,4})-", v.get("time", "") if isinstance(v, dict) else "")
        if m:
            return int(m.group(1))
    return None


def trim(entity: dict) -> dict:
    """The fields this stage reads from a wbgetentities entity."""
    out: dict[str, Any] = {"id": entity.get("id")}
    labels = entity.get("labels", {})
    label = (labels.get("en") or labels.get("mul") or {}).get("value")
    if label:
        out["label"] = label
    claims = entity.get("claims", {})
    live = lambda p: [s for s in claims.get(p, []) if s.get("rank") != "deprecated"]  # noqa: E731
    images = []
    for st in sorted(live("P18"), key=lambda s: s.get("rank") != "preferred"):
        name = _value(st.get("mainsnak", {}))
        if isinstance(name, str) and name:
            quals = st.get("qualifiers", {})
            images.append({"file": name, "year": _year(quals.get("P585", [])),
                           "preferred": st.get("rank") == "preferred"})
    if images:
        out["P18"] = images
    for p in ("P31", "P527"):
        vals = [v.get("id") for s in live(p) for v in [_value(s.get("mainsnak", {}))] if isinstance(v, dict) and v.get("id")]
        if vals:
            out[p] = list(dict.fromkeys(vals))
    cats = [v for s in live("P373") for v in [_value(s.get("mainsnak", {}))] if isinstance(v, str) and v]
    if cats:
        out["P373"] = cats[0]
    for p in ("P569", "P571"):  # born / inception
        y = _year([s.get("mainsnak", {}) for s in live(p)])
        if y:
            out[p] = y
    return out


class Wikimedia:
    def __init__(self, cache_dir: Path | None = None, http=None, *, refresh: bool = False,
                 interval: float | None = None, log: Log = print, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cache_dir = Path(cache_dir or PHOTOS_CACHE)
        self.kv = KVCache(self.cache_dir / "api.sqlite")
        if http is None:
            from ..http import PoliteClient

            iv = WIKIMEDIA_INTERVAL if interval is None else interval
            http = PoliteClient(min_interval={"www.wikidata.org": iv, "commons.wikimedia.org": iv},
                                default_interval=1.0, timeout=90.0, max_retries=5, cache_dir=self.cache_dir / "http")
            http.rate_groups.update({"www.wikidata.org": "wikimedia", "commons.wikimedia.org": "wikimedia"})
        self.http = http
        self.refresh = refresh
        self.log = log
        self.sleep = sleep
        self.requests: Counter = Counter()   # network calls per kind
        self.cached: Counter = Counter()     # cache answers per kind

    # -- plumbing ---------------------------------------------------------------------
    def call(self, api: str, params: dict, kind: str) -> dict:
        """One API request, from the cache when possible (errors are never cached)."""
        params = {**params, "format": "json", "formatversion": "2"}
        key = api + "?" + json.dumps(params, sort_keys=True, separators=(",", ":"))
        if not self.refresh:
            hit = self.kv.get("api", key)
            if hit is not None:
                self.cached[kind] += 1
                return hit
        params["maxlag"] = "5"
        attempt = 0
        while True:
            r = self.http.get(api, params=params, robots=False, use_cache=False)
            self.requests[kind] += 1
            if r.status != 200:
                attempt += 1
                if attempt > 5:
                    raise ApiError(f"HTTP {r.status} from {api}")
                self.sleep(min(120.0, max(_retry_after(r, 0.0), 10.0 * attempt)))
                continue
            data = r.json()
            err = data.get("error")
            if err:
                if err.get("code") == "maxlag" and attempt < 8:
                    attempt += 1
                    self.sleep(min(60.0, max(5.0, _retry_after(r, 5.0))))
                    continue
                raise ApiError(f"{err.get('code')}: {err.get('info')}")
            self.kv.put("api", key, data)
            return data

    # -- Wikidata -----------------------------------------------------------------------
    def entities(self, qids: Iterable[str]) -> dict[str, dict]:
        """QID -> trimmed entity (labels, P18 images, P31, P527, P373, P569/P571); {} when
        missing. A redirected id maps to its target's data."""
        qids = [q for q in dict.fromkeys(qids) if q and re.fullmatch(r"Q\d+", q)]
        out: dict[str, dict] = {}
        for i in range(0, len(qids), BATCH):
            batch = sorted(qids[i:i + BATCH])
            data = self.call(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(batch),
                                            "props": "labels|claims", "languages": "en|mul"}, "wbgetentities")
            ents = data.get("entities", {})
            for q in batch:
                e = ents.get(q)
                if e is None:
                    e = next((v for v in ents.values() if (v.get("redirects") or {}).get("from") == q), None)
                out[q] = {} if e is None or "missing" in e else trim(e)
        return out

    # -- Commons --------------------------------------------------------------------------
    def _imageinfo_params(self) -> dict:
        return {"prop": "imageinfo", "iiprop": "url|extmetadata|size|mime", "iiurlwidth": str(THUMB_WIDTH),
                "iiextmetadatafilter": EXT_FIELDS, "iiextmetadatalanguage": "en"}

    @staticmethod
    def _files(data: dict) -> dict[str, dict]:
        """title -> {"title", "info": imageinfo[0]} of a query answer (missing files left out)."""
        q = data.get("query", {})
        out = {}
        for p in q.get("pages", []):
            info = (p.get("imageinfo") or [None])[0]
            if info and not p.get("missing"):
                out[p["title"]] = {"title": p["title"], "info": info, "index": p.get("index")}
        return out

    def imageinfo(self, titles: Iterable[str]) -> dict[str, dict]:
        """File title ("File:X.jpg" or "X.jpg") -> {"title", "info"}; keys as given."""
        want = {t: t if t.startswith("File:") else "File:" + t for t in dict.fromkeys(titles) if t}
        out: dict[str, dict] = {}
        files = sorted(set(want.values()))
        for i in range(0, len(files), BATCH):
            batch = files[i:i + BATCH]
            data = self.call(COMMONS_API, {"action": "query", "titles": "|".join(batch), **self._imageinfo_params()},
                             "imageinfo")
            q = data.get("query", {})
            alias = {n["from"]: n["to"] for n in q.get("normalized", [])}
            alias.update({r["from"]: r["to"] for r in q.get("redirects", [])})
            got = self._files(data)
            for asked, title in want.items():
                t = alias.get(title, title)
                t = alias.get(t, t)
                if t in got:
                    out[asked] = got[t]
        return out

    def depicts(self, qid: str, limit: int = 30) -> list[dict]:
        """Bitmap files whose structured data says they depict (P180) ``qid``, search order."""
        data = self.call(COMMONS_API, {"action": "query", "generator": "search", "gsrnamespace": "6",
                                       "gsrsearch": f"haswbstatement:P180={qid} filetype:bitmap",
                                       "gsrlimit": str(limit), **self._imageinfo_params()}, "depicts")
        return sorted(self._files(data).values(), key=lambda f: f.get("index") or 0)

    def category_files(self, category: str, limit: int = 500) -> list[str]:
        """Titles of the files directly in a Commons category."""
        cat = category if category.startswith("Category:") else "Category:" + category
        data = self.call(COMMONS_API, {"action": "query", "list": "categorymembers", "cmtitle": cat,
                                       "cmtype": "file", "cmlimit": str(limit)}, "categorymembers")
        return [m["title"] for m in data.get("query", {}).get("categorymembers", []) if m.get("title")]

    # -- files ------------------------------------------------------------------------------
    def download(self, url: str) -> bytes:
        """A thumbnail's bytes (robots-checked, cached under ``data/cache/photos/http``)."""
        r = self.http.get(url, robots=True, use_cache=True)
        self.requests["download" if not getattr(r, "from_cache", False) else "download-cached"] += 1
        if r.status != 200 or not r.content:
            raise ApiError(f"HTTP {r.status} for {url}")
        return r.content


def _retry_after(r, default: float) -> float:
    try:
        return float((getattr(r, "headers", None) or {}).get("Retry-After") or default)
    except (TypeError, ValueError):
        return default
