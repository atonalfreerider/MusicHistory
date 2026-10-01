"""``data/audio/narration/narration.json`` (version 1): the contract between the ``narration``
stage and the Unity viewer's path tour (DESIGN §15). Shape (compact UTF-8)::

    { "version": 1, "voice": "7NoxJCAEPTXbnfIvyaF6", "voice_name": "JohnV4", "model": "eleven_v4",
      "paths": [ { "id": featured path id (as mashups.json),
                   "cues": [ { "id": cue id (lower-case slug),
                               "at": mix seconds the line starts (anchor segment start + offset),
                               "seconds": length of the line's audio,
                               "file": "<path id>/<nn>_<cue id>.wav" (nn = 01, 02, ... in cue order;
                                       relative to data/audio/narration; 48 kHz mono 16-bit, -16 LUFS),
                               "text": the spoken line (declarative sentences ending in periods),
                               "duck_db": music gain under the line, -18..-6 dB,
                               "image": artist image id | null,
                               "sources": [ { "title", "url" } ],
                               "inflection": { "falls": sentence ends that fall, "of": sentences } } ] } ] }

Cues of a path are in time order. Objects carry exactly these keys. ``validate`` checks it all
(with ``root`` the WAV files must exist and be 48 kHz mono; with ``mashups`` every path must be
a mashup and every cue must start inside its mix).
"""

from __future__ import annotations

import re
from pathlib import Path

from . import textshape

VERSION = 1
VOICE = "7NoxJCAEPTXbnfIvyaF6"
VOICE_NAME = "JohnV4"
MODEL = "eleven_v4"
RATE = 48000
DUCK_RANGE = (-18.0, -6.0)
DOC_KEYS = {"version", "voice", "voice_name", "model", "paths"}
PATH_KEYS = {"id", "cues"}
CUE_KEYS = {"id", "at", "seconds", "file", "text", "duck_db", "image", "sources", "inflection"}
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CUE_ID_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
EPS = 1e-3


def cue_file(path_id: str, index: int, cue_id: str) -> str:
    """``<path id>/<nn>_<cue id>.wav`` with nn = index + 1, two digits."""
    return f"{path_id}/{index + 1:02d}_{cue_id}.wav"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _keys(obj, want: set[str], where: str, err: list[str]) -> bool:
    if not isinstance(obj, dict):
        err.append(f"{where}: not an object")
        return False
    if set(obj) != want:
        err.append(f"{where}: keys differ (extra {sorted(set(obj) - want)}, missing {sorted(want - set(obj))})")
        return False
    return True


def validate(doc: dict, root: Path | None = None, mashups: dict | None = None) -> list[str]:
    """Contract violations (empty = valid)."""
    err: list[str] = []
    if not _keys(doc, DOC_KEYS, "doc", err):
        return err
    if doc["version"] != VERSION:
        err.append(f"version {doc['version']!r} != {VERSION}")
    if doc["voice"] != VOICE or doc["voice_name"] != VOICE_NAME or doc["model"] != MODEL:
        err.append("voice / voice_name / model differ from the narrator (JohnV4, eleven_v4)")
    if not isinstance(doc["paths"], list):
        return err + ["paths is not a list"]
    mix = {p["id"]: p for p in mashups.get("paths", [])} if mashups else None
    ids = set()
    for pi, p in enumerate(doc["paths"]):
        w = f"paths[{pi}]"
        if not _keys(p, PATH_KEYS, w, err):
            continue
        pid = p["id"]
        if not (isinstance(pid, str) and SLUG_RE.match(pid)) or pid in ids:
            err.append(f"{w}: bad or duplicate id {pid!r}")
            continue
        ids.add(pid)
        w = f"path {pid}"
        if mix is not None and pid not in mix:
            err.append(f"{w}: no such mashup")
        seconds = float(mix[pid]["seconds"]) if mix is not None and pid in mix else None
        cues = p["cues"]
        if not isinstance(cues, list) or not cues:
            err.append(f"{w}: no cues")
            continue
        cids, last = set(), -1.0
        for ci, c in enumerate(cues):
            wc = f"{w} cue {ci}"
            if not _keys(c, CUE_KEYS, wc, err):
                continue
            cid = c["id"]
            if not (isinstance(cid, str) and CUE_ID_RE.match(cid)) or cid in cids:
                err.append(f"{wc}: bad or duplicate id {cid!r}")
                continue
            cids.add(cid)
            wc = f"{w} cue {cid}"
            if not (_num(c["at"]) and c["at"] >= 0) or c["at"] <= last:
                err.append(f"{wc}: at {c['at']!r} must be >= 0 and after the previous cue")
            elif seconds is not None and c["at"] >= seconds:
                err.append(f"{wc}: at {c['at']} is past the mix end ({seconds})")
            if _num(c["at"]):
                last = float(c["at"])
            if not (_num(c["seconds"]) and 0.2 <= c["seconds"] <= 120):
                err.append(f"{wc}: seconds {c['seconds']!r}")
            want = cue_file(pid, ci, cid)
            if c["file"] != want:
                err.append(f"{wc}: file must be {want!r}")
            elif root is not None:
                f = Path(root) / c["file"]
                if not f.is_file():
                    err.append(f"{wc}: {c['file']} missing")
                else:
                    try:
                        import soundfile

                        info = soundfile.info(str(f))
                        if info.samplerate != RATE or info.channels != 1:
                            err.append(f"{wc}: {c['file']} is {info.samplerate} Hz x{info.channels}, not 48 kHz mono")
                        elif _num(c["seconds"]) and abs(info.duration - c["seconds"]) > 0.02:
                            err.append(f"{wc}: seconds {c['seconds']} != file length {info.duration:.3f}")
                    except RuntimeError as e:
                        err.append(f"{wc}: unreadable WAV ({e})")
            if not textshape.is_shaped(c["text"]):
                err.append(f"{wc}: text must be declarative sentences ending in a period (no ? or !)")
            d = c["duck_db"]
            if not (_num(d) and DUCK_RANGE[0] - EPS <= d <= DUCK_RANGE[1] + EPS):
                err.append(f"{wc}: duck_db {d!r} outside {list(DUCK_RANGE)}")
            img = c["image"]
            if img is not None and not (isinstance(img, str) and img):
                err.append(f"{wc}: image must be an id or null")
            src = c["sources"]
            if not isinstance(src, list):
                err.append(f"{wc}: sources must be a list")
            else:
                for si, s in enumerate(src):
                    if not _keys(s, {"title", "url"}, f"{wc} source {si}", err):
                        continue
                    if not (isinstance(s["title"], str) and s["title"]
                            and isinstance(s["url"], str) and s["url"].startswith(("http://", "https://"))):
                        err.append(f"{wc} source {si}: title and an http(s) url")
            inf = c["inflection"]
            if _keys(inf, {"falls", "of"}, f"{wc} inflection", err):
                f_, o_ = inf["falls"], inf["of"]
                if not (isinstance(f_, int) and isinstance(o_, int) and not isinstance(f_, bool)
                        and not isinstance(o_, bool) and 0 <= f_ <= o_ and o_ >= 1):
                    err.append(f"{wc}: inflection must be 0 <= falls <= of, of >= 1")
    return err
