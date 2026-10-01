"""ElevenLabs text-to-speech with a response cache (DESIGN §15).

Request format as Resonance-2's tutorial narration (``Tools/SongLibrary/narration.py``)::

    POST https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_24000
    headers: xi-api-key, Content-Type: application/json
    body:    {"text", "model_id", "language_code", "voice_settings": {"stability", "similarity_boost"}}

The response is raw 16-bit little-endian mono PCM at 24 kHz.

**The API key** is read from ``KEY_FILE`` only at the moment a request is sent (a cache hit never
touches it) and lives in a local variable for that one request. It is never printed, logged,
cached, written, put into a URL, a cache key or an exception message: every error raised here
is built from the HTTP status alone, and any text that might echo the request is scrubbed.

**Cache**: ``data/cache/narration/<sha256>.pcm`` (+ ``.json`` with the non-secret request
identity), keyed by sha256 of (voice, model, settings, text).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .. import config

VOICE_ID = "7NoxJCAEPTXbnfIvyaF6"
VOICE_NAME = "JohnV4"
MODEL = "eleven_v4"
RATE = 24000
LANGUAGE = "en"
KEY_FILE = Path(os.environ.get("MUSICHISTORY_ELEVEN_KEY_FILE", r"C:\Users\johnb\Desktop\eleven-key.txt"))
API = "https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_{rate}"
TIMEOUT = 180
RETRY_STATUS = {429, 500, 502, 503, 504}

# post(url, body, headers, timeout) -> (status, response bytes). Injected by tests.
Post = Callable[[str, bytes, dict, float], tuple[int, bytes]]


class TTSError(RuntimeError):
    """A speech request failed. Messages never contain credentials or request headers."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def cache_dir() -> Path:
    return config.CACHE / "narration"


@dataclass
class Usage:
    """API usage of one run: requests sent and characters billed (cache hits are free)."""
    requests: int = 0
    characters: int = 0
    cache_hits: int = 0
    log: list = field(default_factory=list)


def request_identity(text: str, settings: dict, voice: str = VOICE_ID, model: str = MODEL,
                     language: str = LANGUAGE) -> str:
    """The cache key: sha256 of (voice, model, settings, text) — no credential, ever."""
    doc = {"voice": voice, "model": model, "language": language,
           "settings": {k: settings[k] for k in sorted(settings)}, "text": text}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_key(key_file: Path) -> str:
    try:
        key = Path(key_file).read_text(encoding="utf-8-sig").strip()
    except OSError:
        raise TTSError(f"cannot read the ElevenLabs key file {Path(key_file).name!r}") from None
    if not key or any(c in key for c in "\r\n\t "):
        raise TTSError("the ElevenLabs key file must contain exactly one key on one line") from None
    return key


def _urllib_post(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:  # noqa: BLE001
            payload = b""
        return int(e.code), payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # str(e) of a URLError holds only the reason (no headers); still, report the type only.
        raise TTSError(f"speech connection failed ({type(e).__name__})") from None


def _error_detail(payload: bytes, key: str | None) -> str:
    """A short, scrubbed reason from an error body (ElevenLabs returns {"detail": {...}})."""
    try:
        doc = json.loads(payload.decode("utf-8", "replace"))
        d = doc.get("detail", doc) if isinstance(doc, dict) else doc
        if isinstance(d, dict):
            text = " ".join(str(d.get(k, "")) for k in ("status", "code", "message") if d.get(k))
        else:
            text = str(d)
    except (ValueError, AttributeError):
        text = ""
    text = text.replace("\n", " ")[:300]
    if key and key in text:
        text = text.replace(key, "[redacted]")
    return text


def synthesize(text: str, settings: dict, *, voice: str = VOICE_ID, model: str = MODEL, language: str = LANGUAGE,
               cache: Path | None = None, post: Post | None = None, key_file: Path | None = None,
               usage: Usage | None = None, max_requests: int | None = None,
               sleep: Callable[[float], None] = time.sleep) -> tuple[np.ndarray, int, str, bool]:
    """Speech for ``text``: (float32 mono samples, rate, cache identity, came from cache).

    Sends a request only on a cache miss. ``max_requests`` caps the requests of a run (the
    ``usage`` counter); exceeding it raises ``TTSError`` before anything is sent."""
    cache = Path(cache) if cache is not None else cache_dir()
    ident = request_identity(text, settings, voice, model, language)
    raw = cache / f"{ident}.pcm"
    if raw.is_file() and raw.stat().st_size >= 2:
        if usage is not None:
            usage.cache_hits += 1
        return _pcm_to_float(raw.read_bytes()), RATE, ident, True
    if usage is not None and max_requests is not None and usage.requests >= max_requests:
        raise TTSError(f"request budget of {max_requests} reached (raise --max-requests)")
    body = json.dumps({"text": text, "model_id": model, "language_code": language,
                       "voice_settings": dict(settings)}).encode("utf-8")
    url = API.format(voice=voice, rate=RATE)
    post = post or _urllib_post
    content = b""
    for attempt in range(4):
        key = _read_key(Path(key_file) if key_file is not None else KEY_FILE)
        headers = {"xi-api-key": key, "Content-Type": "application/json"}
        try:
            status, content = post(url, body, headers, TIMEOUT)
        finally:
            headers = None
        if usage is not None:
            usage.requests += 1
            usage.characters += len(text)
            usage.log.append({"identity": ident, "characters": len(text)})
        if status == 200:
            key = None
            break
        detail = _error_detail(content, key)
        key = None
        if status in RETRY_STATUS and attempt < 3:
            sleep(2.0 * (attempt + 1))
            continue
        hint = " (check the key file)" if status in (401, 403) else ""
        raise TTSError(f"ElevenLabs request failed: HTTP {status}{hint}" + (f": {detail}" if detail else ""),
                       status=status)
    if len(content) < 2 or len(content) % 2:
        raise TTSError(f"ElevenLabs returned {len(content)} bytes, not 16-bit PCM")
    cache.mkdir(parents=True, exist_ok=True)
    tmp = raw.with_suffix(".pcm.tmp")
    tmp.write_bytes(content)
    os.replace(tmp, raw)
    meta = {"voice": voice, "model": model, "language": language, "settings": settings, "text": text,
            "rate": RATE, "format": "pcm_s16le mono", "bytes": len(content)}
    raw.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return _pcm_to_float(content), RATE, ident, False


def _pcm_to_float(data: bytes) -> np.ndarray:
    return np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
