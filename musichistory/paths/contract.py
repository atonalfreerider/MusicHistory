"""``data/audio/renders/paths.json`` (version 2): the contract between the ``paths`` stage and
the Unity walkthrough. Shape (UTF-8)::

    { "version": 2, "generated_at": ISO-8601, "morph_bars": 2,
      "paths": [ { "id": slug, "name", "title", "subtitle", "description", "identity", "seconds",
                   "steps": [ { "work_id", "title", "artist", "year",
                                "file": "<path id>/<nn>_<work_id>.mp3" (relative to renders/),
                                "seconds",
                                "via": null (first step) | { "identity", "strong", "z",
                                                            "edge_kind": "tree"|"secondary",
                                                            "family_size" },
                                "start_key", "key", "start_semitones" (in [-6, 5]),
                                "start_bpm", "bpm", "morph_seconds",
                                "key_source": "audio"|"midi", "bpm_source": "audio"|"midi" } ] } ] }

``start_key`` is the previous step's ``key`` (what is heard when this clip starts), and
``start_semitones = Wrap(tonic(start_key) - tonic(key))``; ``start_bpm`` is the previous
step's ``bpm`` folded toward this one only beyond 0.8 octave; the first step plays natively
(``start_key == key``, ``start_semitones == 0``, ``start_bpm == bpm``, ``morph_seconds == 0``).
``name`` is the path's short display name in Title Case ("Aeolian Rock"), at most 32 characters,
shown top-left in the viewer (which falls back to the Title Case of ``id``). ``validate`` checks
all of it.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .plan import fold_bpm, wrap

VERSION = 2
PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
KEY_RE = re.compile(r"^([A-G])([b#]?) (major|minor)$")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
STEP_KEYS = {"work_id", "title", "artist", "year", "file", "seconds", "via", "start_key", "key", "start_semitones",
             "start_bpm", "bpm", "morph_seconds", "key_source", "bpm_source"}
PATH_KEYS = {"id", "name", "title", "subtitle", "description", "identity", "seconds", "steps"}
NAME_MAX = 32
VIA_KEYS = {"identity", "strong", "z", "edge_kind", "family_size"}


def key_tonic(name: str) -> int:
    m = KEY_RE.match(name or "")
    if not m:
        raise ValueError(f"not a key name: {name!r}")
    return (PITCH[m.group(1)] + (1 if m.group(2) == "#" else -1 if m.group(2) == "b" else 0)) % 12


def step_file(path_id: str, index: int, work_id: str) -> str:
    """Published path of step ``index`` (0-based) relative to data/audio/renders."""
    return f"{path_id}/{index + 1:02d}_{work_id}.mp3"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(doc: dict, renders_dir: Path | None = None) -> list[str]:
    """Contract violations (empty = valid). With ``renders_dir`` the step files must exist."""
    err: list[str] = []
    if doc.get("version") != VERSION:
        err.append(f"version must be {VERSION}")
    try:
        datetime.fromisoformat(str(doc.get("generated_at", "")).replace("Z", "+00:00"))
    except ValueError:
        err.append("generated_at is not ISO-8601")
    if not _num(doc.get("morph_bars")) or doc["morph_bars"] <= 0:
        err.append("morph_bars must be a positive number")
    paths = doc.get("paths")
    if not isinstance(paths, list) or not paths:
        return err + ["paths must be a non-empty list"]
    ids = set()
    for p in paths:
        pid = p.get("id", "?")
        where = f"path {pid}"
        if set(p) != PATH_KEYS:
            err.append(f"{where}: keys {sorted(set(p) ^ PATH_KEYS)} differ from the contract")
        if not isinstance(pid, str) or not SLUG_RE.match(pid):
            err.append(f"{where}: id is not a slug")
        if pid in ids:
            err.append(f"{where}: duplicate id")
        ids.add(pid)
        for f in ("name", "title", "subtitle", "description", "identity"):
            if not isinstance(p.get(f), str) or not p.get(f):
                err.append(f"{where}: {f} must be a non-empty string")
        if isinstance(p.get("name"), str) and (len(p["name"]) > NAME_MAX or p["name"] != p["name"].strip()):
            err.append(f"{where}: name must be at most {NAME_MAX} characters without outer spaces")
        steps = p.get("steps")
        if not isinstance(steps, list) or not (3 <= len(steps) <= 6):
            err.append(f"{where}: needs 3-6 steps")
            continue
        total = 0.0
        prev = None
        for i, s in enumerate(steps):
            w = f"{where} step {i + 1}"
            if set(s) != STEP_KEYS:
                err.append(f"{w}: keys {sorted(set(s) ^ STEP_KEYS)} differ from the contract")
                continue
            if not re.fullmatch(r"Q\d+", str(s["work_id"])):
                err.append(f"{w}: work_id")
            if not isinstance(s["year"], int) or isinstance(s["year"], bool):
                err.append(f"{w}: year must be an int")
            if s["file"] != step_file(pid, i, s["work_id"]):
                err.append(f"{w}: file must be {step_file(pid, i, s['work_id'])}")
            elif renders_dir is not None and not (Path(renders_dir) / s["file"]).is_file():
                err.append(f"{w}: {s['file']} is missing")
            for f in ("seconds", "start_bpm", "bpm", "morph_seconds"):
                if not _num(s[f]) or s[f] < 0 or (f != "morph_seconds" and s[f] <= 0):
                    err.append(f"{w}: {f} must be a positive number")
            if not isinstance(s["start_semitones"], int) or not (-6 <= s["start_semitones"] <= 5):
                err.append(f"{w}: start_semitones must be an int in [-6, 5]")
            for f in ("key_source", "bpm_source"):
                if s[f] not in ("audio", "midi"):
                    err.append(f"{w}: {f} must be audio|midi")
            try:
                t, t0 = key_tonic(s["key"]), key_tonic(s["start_key"])
            except ValueError as exc:
                err.append(f"{w}: {exc}")
                prev = s
                continue
            if isinstance(s["seconds"], (int, float)):
                total += s["seconds"]
            if i == 0:
                if s["via"] is not None:
                    err.append(f"{w}: via must be null on the first step")
                if s["start_key"] != s["key"] or s["start_semitones"] != 0 or s["morph_seconds"] != 0 \
                        or abs(s["start_bpm"] - s["bpm"]) > 1e-6:
                    err.append(f"{w}: the first step must play natively")
            else:
                via = s["via"]
                if not isinstance(via, dict) or set(via) != VIA_KEYS:
                    err.append(f"{w}: via must have {sorted(VIA_KEYS)}")
                else:
                    if via["edge_kind"] not in ("tree", "secondary"):
                        err.append(f"{w}: via.edge_kind")
                    if not isinstance(via["strong"], bool) or not _num(via["z"]) \
                            or not isinstance(via["family_size"], int) or not via["identity"]:
                        err.append(f"{w}: via field types")
                if prev is not None and s["start_key"] != prev.get("key"):
                    err.append(f"{w}: start_key must be the previous step's key")
                if s["start_semitones"] != wrap(t0 - t):
                    err.append(f"{w}: start_semitones must be Wrap(start tonic - tonic) = {wrap(t0 - t)}")
                if prev is not None and _num(prev.get("bpm")) and _num(s["bpm"]):
                    want = fold_bpm(prev["bpm"], s["bpm"])
                    if abs(s["start_bpm"] - want) > 0.011:
                        err.append(f"{w}: start_bpm must be the previous bpm folded ({want:.2f})")
                if not (_num(s["morph_seconds"]) and s["morph_seconds"] > 0):
                    err.append(f"{w}: morph_seconds must be positive after the first step")
            prev = s
        if not _num(p.get("seconds")) or abs(p["seconds"] - total) > 0.05:
            err.append(f"{where}: seconds must be the sum of the steps ({total:.2f})")
    return err
