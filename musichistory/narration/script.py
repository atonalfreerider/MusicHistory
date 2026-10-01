"""Narration scripts (``musichistory/narration/scripts/<path id>.json``, DESIGN §15) as the
narration stage reads them: structure check, cue times on the mix, and the fit estimate.

Shape::

    {"id": path id, "title", "cues": [{"id", "anchor": {"segment": int, "offset": seconds},
      "kind": "intro"|"song"|"changeover"|"outro", "text", "image": str|null,
      "sources": [{"title", "url"}]}]}
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import textshape

SCRIPTS = Path(__file__).resolve().parent / "scripts"
KINDS = {"intro", "song", "changeover", "outro"}
CUE_ID_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
WORDS_PER_SECOND = 2.6


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def script_files(ids: list[str] | None = None, folder: Path = SCRIPTS) -> list[Path]:
    files = sorted(Path(folder).glob("*.json")) if Path(folder).is_dir() else []
    return [f for f in files if ids is None or f.stem in ids]


def cue_time(cue: dict, mashup: dict) -> float:
    """Mix seconds of a cue: its anchor segment's start plus the anchor offset."""
    seg = mashup["segments"][cue["anchor"]["segment"]]
    return round(float(seg["start"]) + float(cue["anchor"]["offset"]), 3)


def estimate_seconds(text: str) -> float:
    return textshape.words(text) / WORDS_PER_SECOND


def validate(doc: dict, mashup: dict | None = None) -> list[str]:
    """Errors in a script (empty = usable). With the path's ``mashup`` entry, anchors must point
    at its segments and cue times must increase and lie inside the mix."""
    err: list[str] = []
    if not isinstance(doc, dict):
        return ["script is not an object"]
    for k in ("id", "title", "cues"):
        if k not in doc:
            err.append(f"missing {k!r}")
    if err:
        return err
    if not isinstance(doc["cues"], list) or not doc["cues"]:
        return ["cues must be a non-empty list"]
    ids, last_at = set(), -1.0
    nseg = len(mashup["segments"]) if mashup else None
    for i, c in enumerate(doc["cues"]):
        w = f"cue {i}"
        if not isinstance(c, dict):
            err.append(f"{w}: not an object")
            continue
        for k in ("id", "anchor", "kind", "text", "image", "sources"):
            if k not in c:
                err.append(f"{w}: missing {k!r}")
        if any(k not in c for k in ("id", "anchor", "text")):
            continue
        w = f"cue {i} ({c['id']})"
        if not (isinstance(c["id"], str) and CUE_ID_RE.match(c["id"])) or c["id"] in ids:
            err.append(f"{w}: id must be a unique lower-case slug")
        ids.add(c["id"])
        if c.get("kind") not in KINDS:
            err.append(f"{w}: kind {c.get('kind')!r}")
        if not isinstance(c["text"], str) or not textshape.words(c["text"]):
            err.append(f"{w}: empty text")
        a = c["anchor"]
        if not (isinstance(a, dict) and isinstance(a.get("segment"), int) and not isinstance(a.get("segment"), bool)
                and isinstance(a.get("offset"), (int, float)) and not isinstance(a.get("offset"), bool)
                and a["offset"] >= 0):
            err.append(f"{w}: anchor must be {{segment: int, offset: seconds >= 0}}")
            continue
        if nseg is not None:
            if not 0 <= a["segment"] < nseg:
                err.append(f"{w}: anchor segment {a['segment']} outside 0..{nseg - 1}")
                continue
            at = cue_time(c, mashup)
            if at <= last_at:
                err.append(f"{w}: starts at {at:.2f}s, not after the previous cue ({last_at:.2f}s)")
            if at >= float(mashup["seconds"]):
                err.append(f"{w}: starts at {at:.2f}s, after the mix ends ({mashup['seconds']}s)")
            last_at = at
        img = c.get("image")
        if img is not None and not (isinstance(img, str) and img):
            err.append(f"{w}: image must be an id or null")
        src = c.get("sources")
        if not isinstance(src, list) or not all(isinstance(s, dict) and isinstance(s.get("title"), str)
                                                and isinstance(s.get("url"), str)
                                                and s["url"].startswith(("http://", "https://")) for s in src):
            err.append(f"{w}: sources must be [{{title, url}}] with http(s) urls")
    return err


def fit_plan(doc: dict, mashup: dict) -> list[dict]:
    """Per cue: start, estimated speech seconds (2.6 words/s) and the room before the next cue
    (or the mix end)."""
    cues = doc["cues"]
    ats = [cue_time(c, mashup) for c in cues]
    out = []
    for i, c in enumerate(cues):
        room = (ats[i + 1] if i + 1 < len(cues) else float(mashup["seconds"])) - ats[i]
        est = estimate_seconds(textshape.shape(c["text"]))
        out.append({"id": c["id"], "at": ats[i], "estimate": round(est, 2), "room": round(room, 2),
                    "fits": est <= room})
    return out
