"""A polite HTTP client shared by every network stage.

* one ``requests.Session`` per host, a fixed minimum interval between requests to a host
  (with jitter), and exponential backoff that honours ``Retry-After`` on 429/5xx;
* robots.txt is fetched once per host and checked before every request (``robots=False``
  only for API endpoints whose etiquette is documented separately, e.g. the MediaWiki API);
* optional on-disk cache keyed by URL, so re-running a stage does not re-download;
* every response is logged to the ``fetch_log`` table when a connection is supplied.

The User-Agent never contains personal data unless MUSICHISTORY_CONTACT is set.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import config


class RobotsDisallowed(RuntimeError):
    pass


class Robots:
    """robots.txt rules with the ``*`` and ``$`` wildcards Google and most sites use.

    ``urllib.robotparser`` treats ``Disallow: /*?q=`` literally, so it would allow
    midicollection's search. Here the group for our agent token (else ``*``) applies, the
    longest matching rule wins and Allow wins ties, per RFC 9309.
    """

    def __init__(self, text: str, agent_token: str = "musichistory") -> None:
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (p.strip() for p in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if rules:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
            elif field in ("allow", "disallow") and agents:
                if value:
                    rules.append((field == "allow", value))
                elif field == "disallow":
                    rules.append((True, "/"))  # "Disallow:" (empty) allows everything
        if agents:
            groups.append((agents, rules))
        token = agent_token.lower()
        chosen = [r for a, r in groups if any(x != "*" and x in token for x in a)]
        if not chosen:
            chosen = [r for a, r in groups if "*" in a]
        self.rules = [rule for group in chosen for rule in group]

    @staticmethod
    def _pattern(path: str):
        import re

        anchored = path.endswith("$")
        body = re.escape(path[:-1] if anchored else path).replace(r"\*", ".*")
        return re.compile(body + ("$" if anchored else ""))

    def can_fetch(self, url: str) -> bool:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool] | None = None
        for allow, path in self.rules:
            if self._pattern(path).match(target):
                key = (len(path), allow)
                if best is None or key > best:
                    best = key
        return True if best is None else best[1]


@dataclass
class Fetched:
    url: str
    status: int
    content: bytes
    content_type: str
    headers: dict
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        import json

        return json.loads(self.content)


class PoliteClient:
    """Rate-limited, robots-aware HTTP client.

    ``min_interval`` maps a host (e.g. ``"freemidi.org"``) to seconds between requests;
    ``default_interval`` applies to every other host.
    """

    def __init__(
        self,
        min_interval: dict[str, float] | None = None,
        default_interval: float = 1.0,
        timeout: float = 30.0,
        max_retries: int = 4,
        cache_dir: Path | None = None,
        db: sqlite3.Connection | None = None,
    ) -> None:
        self.min_interval = dict(min_interval or {})
        self.default_interval = default_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache_dir = cache_dir
        self.db = db
        self._sessions: dict[str, requests.Session] = {}
        self._last: dict[str, float] = {}
        self._robots: dict[str, Robots | None] = {}
        self._lock = threading.Lock()
        # Hosts that share one rate budget (e.g. Wikipedia and Wikidata): host -> group key.
        self.rate_groups: dict[str, str] = {}

    # -- plumbing ---------------------------------------------------------------------
    def session(self, host: str) -> requests.Session:
        s = self._sessions.get(host)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = config.user_agent()
            self._sessions[host] = s
        return s

    def _wait(self, host: str) -> None:
        interval = self.min_interval.get(host, self.default_interval)
        key = self.rate_groups.get(host, host)
        with self._lock:
            last = self._last.get(key, 0.0)
            delay = last + interval * random.uniform(1.0, 1.25) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last[key] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.netloc
        if host not in self._robots:
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            try:
                self._wait(host)
                r = self.session(host).get(robots_url, timeout=self.timeout)
                ctype = r.headers.get("Content-Type", "")
                # Soft-404 pages (HTML served with 200) state no rules.
                if r.status_code == 200 and "html" not in ctype.lower():
                    self._robots[host] = Robots(r.text)
                else:
                    self._robots[host] = None
            except requests.RequestException:
                self._robots[host] = None
        rules = self._robots[host]
        return True if rules is None else rules.can_fetch(url)

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        h = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / h[:2] / h

    def _log(self, url: str, status: int, ctype: str, content: bytes, elapsed_ms: float) -> None:
        if self.db is None:
            return
        self.db.execute(
            "INSERT INTO fetch_log(url, status, content_type, bytes, sha256, elapsed_ms, fetched_at)"
            " VALUES (?,?,?,?,?,?,datetime('now'))",
            (url, status, ctype, len(content), hashlib.sha256(content).hexdigest(), elapsed_ms),
        )
        self.db.commit()

    # -- public -----------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        robots: bool = True,
        use_cache: bool = True,
        allow_redirects: bool = True,
        ok_statuses: tuple[int, ...] = (200,),
        cache_if: "Callable[[Fetched], bool] | None" = None,
    ) -> Fetched:
        """GET ``url`` politely. ``cache_if`` decides whether a successful response may be
        cached (e.g. MediaWiki reports errors with HTTP 200)."""
        full = requests.Request("GET", url, params=params).prepare().url
        cache = self._cache_path(full) if use_cache else None
        if cache is not None and cache.exists():
            data = cache.read_bytes()
            ctype_path = cache.with_suffix(".ctype")
            ctype = ctype_path.read_text() if ctype_path.exists() else ""
            return Fetched(full, 200, data, ctype, {}, from_cache=True)
        if robots and not self.allowed(full):
            raise RobotsDisallowed(full)
        host = urlsplit(full).netloc
        attempt = 0
        while True:
            self._wait(host)
            t0 = time.monotonic()
            try:
                r = self.session(host).get(
                    full, headers=headers, timeout=self.timeout, allow_redirects=allow_redirects
                )
            except requests.RequestException:
                attempt += 1
                if attempt > self.max_retries:
                    raise
                time.sleep(min(60, 2**attempt))
                continue
            elapsed = (time.monotonic() - t0) * 1000
            ctype = r.headers.get("Content-Type", "")
            self._log(full, r.status_code, ctype, r.content, elapsed)
            if r.status_code == 429 or r.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    return Fetched(full, r.status_code, r.content, ctype, dict(r.headers))
                retry_after = r.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    wait = 2**attempt
                time.sleep(min(120, wait))
                continue
            fetched = Fetched(full, r.status_code, r.content, ctype, dict(r.headers))
            if cache is not None and r.status_code in ok_statuses and (cache_if is None or cache_if(fetched)):
                cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_suffix(".tmp")
                tmp.write_bytes(r.content)
                tmp.replace(cache)
                cache.with_suffix(".ctype").write_text(ctype)
            return fetched


def download(url: str, dest: Path, *, expected_md5: str | None = None, chunk: int = 1 << 20) -> Path:
    """Stream a large file to ``dest`` with resume support (Range) and optional MD5 check."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists():
        return dest
    headers = {"User-Agent": config.user_agent()}
    have = part.stat().st_size if part.exists() else 0
    if have:
        headers["Range"] = f"bytes={have}-"
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        if have and r.status_code != 206:
            have = 0
            part.unlink(missing_ok=True)
        r.raise_for_status()
        with open(part, "ab" if have else "wb") as f:
            for block in r.iter_content(chunk):
                f.write(block)
    if expected_md5:
        h = hashlib.md5()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 22), b""):
                h.update(block)
        if h.hexdigest() != expected_md5:
            part.unlink(missing_ok=True)
            raise ValueError(f"MD5 mismatch for {url}")
    part.replace(dest)
    return dest
