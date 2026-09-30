"""Network access and caches for the canon stage.

Two caches live under ``data/cache/canon/``:

* ``raw/``: every list download (CSV/TSV files, Wikipedia page wikitext) as the bytes
  received, next to a ``.meta.json`` sidecar with URL, revision/commit, SHA-256 and
  retrieval time. ``source_list`` rows are filled from these sidecars, so a rerun that
  reuses the cache still reports where and when the data came from.
* ``api_cache.sqlite``: one row per API answer (Wikipedia pageprops/search, Wikidata
  entities, MusicBrainz searches), written only after a successful response. Every API
  step reads it first, which makes the stage resumable: a run that is interrupted or
  times out continues where it stopped.

Etiquette (see docs/DESIGN.md): requests are sequential. Wikimedia (Wikipedia and
Wikidata share one budget) ~0.7 s apart, with ``maxlag=5`` on Wikidata (a lag error waits
``Retry-After`` and retries). Wikimedia's API rate limits give a User-Agent without contact
information only 10 requests per minute (https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits),
so unless ``MUSICHISTORY_CONTACT`` is set the shared interval is 6.5 s; that is why every
step batches 50 titles or items per request and caches every answer. MusicBrainz is
>= 1.1 s apart, tsort.info >= 12 s apart. The MediaWiki and MusicBrainz web
services publish their own API etiquette, which is followed instead of the crawler
robots.txt (as ``musichistory.http`` documents for API endpoints); plain file downloads
check robots.txt.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .. import config
from ..http import PoliteClient

CANON_CACHE = config.CACHE / "canon"

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
MUSICBRAINZ_WS = "https://musicbrainz.org/ws/2/"

# Seconds between any two Wikimedia requests (Wikipedia + Wikidata share one limit).
WIKIMEDIA_INTERVAL = 0.7 if config.CONTACT else 6.5

INTERVALS = {
    "en.wikipedia.org": 0.6,
    "www.wikidata.org": 0.8,
    "musicbrainz.org": 1.1,
    "tsort.info": 12.0,
    "raw.githubusercontent.com": 1.0,
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ApiError(RuntimeError):
    pass


class KVCache:
    """A tiny namespaced JSON key-value store in SQLite (safe to interrupt at any point)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=60)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS kv(ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
            " fetched_at TEXT, PRIMARY KEY(ns, key))"
        )
        self.conn.commit()

    def get(self, ns: str, key: str) -> Any | None:
        row = self.conn.execute("SELECT value FROM kv WHERE ns = ? AND key = ?", (ns, key)).fetchone()
        return json.loads(row[0]) if row else None

    def get_many(self, ns: str, keys: Iterable[str]) -> dict[str, Any]:
        keys = list(dict.fromkeys(keys))
        out: dict[str, Any] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            q = f"SELECT key, value FROM kv WHERE ns = ? AND key IN ({','.join('?' * len(chunk))})"
            for k, v in self.conn.execute(q, (ns, *chunk)):
                out[k] = json.loads(v)
        return out

    def put(self, ns: str, key: str, value: Any, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kv(ns, key, value, fetched_at) VALUES (?,?,?,?)",
            (ns, key, json.dumps(value, separators=(",", ":"), ensure_ascii=False), now_iso()),
        )
        if commit:
            self.conn.commit()

    def put_many(self, ns: str, items: dict[str, Any]) -> None:
        ts = now_iso()
        self.conn.executemany(
            "INSERT OR REPLACE INTO kv(ns, key, value, fetched_at) VALUES (?,?,?,?)",
            [(ns, k, json.dumps(v, separators=(",", ":"), ensure_ascii=False), ts) for k, v in items.items()],
        )
        self.conn.commit()

    def count(self, ns: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM kv WHERE ns = ?", (ns,)).fetchone()[0]

    def close(self) -> None:
        self.conn.close()


class Net:
    """Polite access to every service the canon stage uses, with caching."""

    def __init__(self, cache_root: Path | None = None, *, offline: bool = False) -> None:
        self.root = Path(cache_root or CANON_CACHE)
        self.raw_dir = self.root / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.kv = KVCache(self.root / "api_cache.sqlite")
        self.http = PoliteClient(min_interval=INTERVALS, default_interval=1.0, timeout=90.0, max_retries=5)
        self.offline = offline
        self.requests = 0
        self._wm_last = 0.0

    def _wikimedia_wait(self) -> None:
        delay = self._wm_last + WIKIMEDIA_INTERVAL - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._wm_last = time.monotonic()

    # -- raw list files -----------------------------------------------------------------
    def raw_path(self, name: str) -> Path:
        return self.raw_dir / name

    def raw_meta(self, name: str) -> dict | None:
        p = self.raw_dir / (name + ".meta.json")
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def store_raw(self, name: str, data: bytes, meta: dict) -> dict:
        path = self.raw_dir / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        meta = dict(meta)
        meta.setdefault("retrieved_at", now_iso())
        meta["sha256"] = sha256_bytes(data)
        meta["bytes"] = len(data)
        (self.raw_dir / (name + ".meta.json")).write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return meta

    def fetch_file(self, name: str, url: str, *, revision: str | None = None, refresh: bool = False) -> tuple[bytes, dict]:
        """Download ``url`` once into ``raw/<name>`` (robots.txt honoured)."""
        path = self.raw_dir / name
        meta = self.raw_meta(name)
        if path.exists() and meta and not refresh:
            return path.read_bytes(), meta
        if self.offline:
            raise ApiError(f"offline and {name} is not cached")
        r = self.http.get(url, use_cache=False)
        self.requests += 1
        if r.status != 200:
            raise ApiError(f"HTTP {r.status} for {url}")
        meta = self.store_raw(name, r.content, {"url": url, "revision": revision})
        return r.content, meta

    # -- MediaWiki / Wikidata --------------------------------------------------------------
    def _mw(self, api: str, params: dict) -> dict:
        if self.offline:
            raise ApiError("offline")
        params = {**params, "format": "json", "formatversion": "2"}
        if "wikidata" in api:
            params["maxlag"] = "5"
        attempt = 0
        while True:
            self._wikimedia_wait()
            r = self.http.get(api, params=params, robots=False, use_cache=False)
            self.requests += 1
            if r.status != 200:
                attempt += 1
                if attempt > 5:
                    raise ApiError(f"HTTP {r.status} from {api}")
                try:
                    wait = float(r.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0.0
                time.sleep(min(120, max(wait, 10 * attempt)))
                continue
            data = r.json()
            err = data.get("error")
            if err:
                if err.get("code") == "maxlag" and attempt < 8:
                    attempt += 1
                    wait = float(r.headers.get("Retry-After", 5) or 5)
                    time.sleep(min(60, max(5.0, wait)))
                    continue
                raise ApiError(f"{err.get('code')}: {err.get('info')}")
            return data

    def wikipedia(self, params: dict) -> dict:
        return self._mw(WIKIPEDIA_API, params)

    def wikidata(self, params: dict) -> dict:
        return self._mw(WIKIDATA_API, params)

    # -- MusicBrainz ----------------------------------------------------------------------
    def musicbrainz(self, entity: str, params: dict) -> dict:
        if self.offline:
            raise ApiError("offline")
        params = {**params, "fmt": "json"}
        attempt = 0
        while True:
            r = self.http.get(MUSICBRAINZ_WS + entity, params=params, robots=False, use_cache=False,
                              headers={"Accept": "application/json"})
            self.requests += 1
            if r.status == 200:
                return r.json()
            if r.status in (400, 404):
                return {}
            attempt += 1
            if attempt > 4:
                raise ApiError(f"HTTP {r.status} from MusicBrainz")
            time.sleep(min(60, 5 * attempt))
