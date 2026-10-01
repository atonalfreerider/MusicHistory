"""``data/audio/mashups/mashups.json`` (version 1): the contract between the ``mashup`` stage and
the Unity viewer's mashup player and melody graph. Shape (UTF-8)::

    { "version": 1, "generated_at": ISO-8601,
      "frame": "C major / A minor (relative normalization, as the pipeline)",
      "paths": [ { "id": featured path id (as paths.json), "title",
                   "file": "<id>/mix.mp3" (relative to data/audio/mashups), "seconds",
                   "beats_per_bar": int, "phrase_beats": float (phrase bars * beats_per_bar),
                   "segments": [ { "start", "end" (mix seconds; contiguous from 0 to seconds),
                                   "kind": "full" | "changeover" | "morph",
                                   "instrumental": work_id, "vocal": work_id | null,
                                   "key": key heard ("C major"), "bpm" (heard; a morph's end value),
                                   "bpm_start", "vocal_shift_semitones": int | null,
                                   "vocal_tempo_ratio": float | null, "chord_match": 0..1 | null,
                                   "beat_error_ms": float | null } ],
                   "beats": [ [mix seconds, phrase beat], ... ]  (every beat; 0 <= phrase beat < phrase_beats),
                   "songs": [ { "work_id", "title", "artist", "year", "step" (0-based order),
                                "melody": [ [phrase beat, MIDI pitch | null], ... ],
                                "chords": [ [start beat, end beat, root pc 0..11, quality, roman], ... ],
                                "vocal_audible": [ [start, end], ... ],
                                "instrumental_audible": [ [start, end], ... ] } ] } ] }

Melody pitches and chord roots are in the normalized C major / A minor frame; a melody has at
most one point per 1/16 beat and ``null`` marks an unvoiced break (and a wrap of the phrase).
A changeover's ``vocal`` differs from its ``instrumental`` and carries ``chord_match``; a full
segment plays one song (``vocal`` is that song or null). Objects carry exactly the keys above:
there is no free text beyond ids, titles, artists, key names and roman numerals (never lyrics).
``validate`` checks all of it.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

VERSION = 1
FRAME = "C major / A minor (relative normalization, as the pipeline)"
KINDS = {"full", "changeover", "morph"}
QUALITIES = {"maj", "min", "dim", "aug", "sus", "other"}
KEY_RE = re.compile(r"^[A-G][b#]? (major|minor)$")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ROMAN_RE = re.compile(r"^[b#]?(I|II|III|IV|V|VI|VII|i|ii|iii|iv|v|vi|vii)(o|\+|sus)?$")
DOC_KEYS = {"version", "generated_at", "frame", "paths"}
PATH_KEYS = {"id", "title", "file", "seconds", "beats_per_bar", "phrase_beats", "segments", "beats", "songs"}
SEG_KEYS = {"start", "end", "kind", "instrumental", "vocal", "key", "bpm", "bpm_start", "vocal_shift_semitones",
            "vocal_tempo_ratio", "chord_match", "beat_error_ms"}
SONG_KEYS = {"work_id", "title", "artist", "year", "step", "melody", "chords", "vocal_audible",
             "instrumental_audible"}
EPS = 1e-3


def mix_file(path_id: str) -> str:
    return f"{path_id}/mix.mp3"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _keys(obj, want: set[str], where: str, err: list[str]) -> bool:
    if not isinstance(obj, dict):
        err.append(f"{where}: not an object")
        return False
    if set(obj) != want:
        extra, missing = sorted(set(obj) - want), sorted(want - set(obj))
        err.append(f"{where}: keys differ (extra {extra}, missing {missing})")
        return False
    return True


def _intervals(v, seconds: float, where: str, err: list[str]) -> None:
    if not isinstance(v, list):
        err.append(f"{where}: not a list")
        return
    last = -1.0
    for k, iv in enumerate(v):
        if not (isinstance(iv, list) and len(iv) == 2 and all(_num(x) for x in iv)):
            err.append(f"{where}[{k}]: not [start, end]")
            continue
        a, b = iv
        if not (0 <= a < b <= seconds + EPS) or a < last - EPS:
            err.append(f"{where}[{k}]: bad interval {iv}")
        last = b


def validate(doc: dict, root: Path | None = None) -> list[str]:
    """Contract violations (empty = valid). With ``root`` (data/audio/mashups) the files must exist."""
    err: list[str] = []
    if not _keys(doc, DOC_KEYS, "doc", err):
        return err
    if doc["version"] != VERSION:
        err.append(f"version {doc['version']!r} != {VERSION}")
    try:
        datetime.fromisoformat(str(doc["generated_at"]).replace("Z", "+00:00"))
    except ValueError:
        err.append("generated_at is not ISO-8601")
    if doc["frame"] != FRAME:
        err.append("frame differs")
    if not isinstance(doc["paths"], list):
        return err + ["paths is not a list"]
    ids = set()
    for pi, p in enumerate(doc["paths"]):
        w = f"paths[{pi}]"
        if not _keys(p, PATH_KEYS, w, err):
            continue
        pid = p["id"]
        if not (isinstance(pid, str) and SLUG_RE.match(pid)) or pid in ids:
            err.append(f"{w}: bad or duplicate id {pid!r}")
        ids.add(pid)
        w = f"path {pid}"
        if not isinstance(p["title"], str) or not p["title"]:
            err.append(f"{w}: title")
        if p["file"] != mix_file(str(pid)):
            err.append(f"{w}: file must be {mix_file(str(pid))!r}")
        elif root is not None and not (Path(root) / p["file"]).is_file():
            err.append(f"{w}: {p['file']} missing")
        seconds = p["seconds"]
        if not _num(seconds) or seconds <= 0:
            err.append(f"{w}: seconds")
            continue
        bpb = p["beats_per_bar"]
        if not isinstance(bpb, int) or isinstance(bpb, bool) or not 2 <= bpb <= 12:
            err.append(f"{w}: beats_per_bar")
            bpb = 4
        pb = p["phrase_beats"]
        if not _num(pb) or pb <= 0 or abs(pb / bpb - round(pb / bpb)) > EPS:
            err.append(f"{w}: phrase_beats must be a positive whole number of bars")
            pb = 1e9
        # segments
        segs = p["segments"]
        song_ids = {s.get("work_id") for s in p["songs"]} if isinstance(p["songs"], list) else set()
        if not isinstance(segs, list) or not segs:
            err.append(f"{w}: no segments")
            segs = []
        prev_end = 0.0
        for si, s in enumerate(segs):
            ws = f"{w} segment {si}"
            if not _keys(s, SEG_KEYS, ws, err):
                continue
            if not (_num(s["start"]) and _num(s["end"]) and s["start"] < s["end"]):
                err.append(f"{ws}: start/end")
                continue
            if abs(s["start"] - prev_end) > EPS:
                err.append(f"{ws}: starts at {s['start']}, previous ended at {prev_end}")
            prev_end = s["end"]
            if s["kind"] not in KINDS:
                err.append(f"{ws}: kind {s['kind']!r}")
            if s["instrumental"] not in song_ids or (s["vocal"] is not None and s["vocal"] not in song_ids):
                err.append(f"{ws}: instrumental/vocal not among the path's songs")
            if s["kind"] == "changeover":
                if s["vocal"] is None or s["vocal"] == s["instrumental"]:
                    err.append(f"{ws}: a changeover needs another song's vocal")
                if not (_num(s["chord_match"]) and 0 <= s["chord_match"] <= 1):
                    err.append(f"{ws}: changeover chord_match must be in 0..1")
            elif s["vocal"] not in (None, s["instrumental"]):
                err.append(f"{ws}: a {s['kind']} segment plays one song")
            if not (isinstance(s["key"], str) and KEY_RE.match(s["key"])):
                err.append(f"{ws}: key {s['key']!r}")
            for k in ("bpm", "bpm_start"):
                if not (_num(s[k]) and 20 <= s[k] <= 400):
                    err.append(f"{ws}: {k}")
            v = s["vocal_shift_semitones"]
            if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and -6 <= v <= 6):
                err.append(f"{ws}: vocal_shift_semitones")
            r = s["vocal_tempo_ratio"]
            if r is not None and not (_num(r) and 0.25 <= r <= 4):
                err.append(f"{ws}: vocal_tempo_ratio")
            c = s["chord_match"]
            if c is not None and not (_num(c) and 0 <= c <= 1):
                err.append(f"{ws}: chord_match")
            b = s["beat_error_ms"]
            if b is not None and not (_num(b) and b >= 0):
                err.append(f"{ws}: beat_error_ms")
        if segs and abs(prev_end - seconds) > 0.05:
            err.append(f"{w}: segments end at {prev_end}, mix lasts {seconds}")
        # beats
        beats = p["beats"]
        if not isinstance(beats, list) or len(beats) < 2:
            err.append(f"{w}: beats")
        else:
            last = -1.0
            for bi, b in enumerate(beats):
                if not (isinstance(b, list) and len(b) == 2 and _num(b[0]) and _num(b[1])):
                    err.append(f"{w}: beats[{bi}] malformed")
                    break
                if b[0] <= last or b[0] < 0 or b[0] > seconds + EPS or not (0 <= b[1] < pb):
                    err.append(f"{w}: beats[{bi}] = {b} out of order or range")
                    break
                last = b[0]
        # songs
        songs = p["songs"]
        if not isinstance(songs, list) or not songs:
            err.append(f"{w}: no songs")
            continue
        for k, so in enumerate(songs):
            wo = f"{w} song {k}"
            if not _keys(so, SONG_KEYS, wo, err):
                continue
            if so["step"] != k:
                err.append(f"{wo}: step {so['step']} != {k}")
            for f in ("work_id", "title", "artist"):
                if not isinstance(so[f], str) or not so[f]:
                    err.append(f"{wo}: {f}")
            if not isinstance(so["year"], int) or isinstance(so["year"], bool):
                err.append(f"{wo}: year")
            mel = so["melody"]
            if not isinstance(mel, list):
                err.append(f"{wo}: melody")
            else:
                prev = None
                for mi, pt in enumerate(mel):
                    if not (isinstance(pt, list) and len(pt) == 2 and _num(pt[0])
                            and (pt[1] is None or _num(pt[1]))):
                        err.append(f"{wo}: melody[{mi}] malformed")
                        break
                    if not 0 <= pt[0] < pb or (pt[1] is not None and not 20 <= pt[1] <= 110):
                        err.append(f"{wo}: melody[{mi}] = {pt} out of range")
                        break
                    if prev is not None and pt[1] is not None and prev[1] is not None and \
                            0 <= pt[0] - prev[0] < 1 / 16 - 1e-6:
                        err.append(f"{wo}: melody denser than 1/16 beat at {mi}")
                        break
                    prev = pt
            chords = so["chords"]
            if not isinstance(chords, list):
                err.append(f"{wo}: chords")
            else:
                for ci, ch in enumerate(chords):
                    if not (isinstance(ch, list) and len(ch) == 5 and _num(ch[0]) and _num(ch[1])
                            and isinstance(ch[2], int) and ch[3] in QUALITIES and isinstance(ch[4], str)
                            and ROMAN_RE.match(ch[4])):
                        err.append(f"{wo}: chords[{ci}] malformed: {ch}")
                        break
                    if not (0 <= ch[0] < ch[1] <= pb + EPS and 0 <= ch[2] <= 11):
                        err.append(f"{wo}: chords[{ci}] out of range: {ch}")
                        break
            _intervals(so["vocal_audible"], seconds, f"{wo} vocal_audible", err)
            _intervals(so["instrumental_audible"], seconds, f"{wo} instrumental_audible", err)
    return err
