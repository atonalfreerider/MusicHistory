"""``data/audio/duets/duets.json`` (version 1): the contract between the ``duets`` stage and the
Unity viewer's duet-loop player and melody graph (DESIGN §16). Shape (UTF-8)::

    { "version": 1, "generated_at": ISO-8601,
      "frame": "C major / A minor (relative normalization, as the pipeline)",
      "paths": [ { "id": featured path id (as paths.json), "title",
                   "file": "<id>/loop.mp3" (relative to data/audio/duets), "seconds",
                   "loops": true (the file's end runs on into its start),
                   "root": work_id of the root song S0, "key": the root's key ("C major"),
                   "bpm": the loop's tempo (the root's), "beats_per_bar": int,
                   "phrase_beats": float (phrase bars * beats_per_bar),
                   "segments": [ { "start", "end" (seconds; contiguous from 0 to seconds),
                                   "kind": "duet" | "handoff",
                                   "instrumental": work_id (the root in duets, the borrowed song in handoffs),
                                   "vocals": [work_id, work_id] (the pair singing at the segment's end),
                                   "entering": work_id | null, "leaving": work_id | null (handoffs only),
                                   "chord_match": 0..1 } ],
                   "beats": [ [mix seconds, mix beat], ... ]  (every beat; mix beats 0, 1, 2, ... increasing),
                   "chords": [ [start beat, end beat, root pc 0..11, quality, roman], ... ]
                             (what the instrumental plays, in mix beats),
                   "songs": [ { "work_id", "title", "artist", "year", "step" (0-based order),
                                "shift_semitones": int, "tempo_ratio": float,
                                "melody": [ [mix beat, MIDI pitch | null], ... ],
                                "vocal_audible": [ [start, end], ... ],
                                "instrumental_audible": [ [start, end], ... ] } ] } ] }

Melody pitches and chord roots are in the normalized C major / A minor frame, as heard; a
melody has at most one point per 1/16 beat and ``null`` marks a break. Audible intervals are
in seconds. A duet segment's vocals are the pair it plays (no entering/leaving); a handoff's
entering song is one of its vocals and its leaving song is not, and its instrumental is the
entering song's (the leaving song's when the entering song is the root). Every song sings in
exactly two consecutive pairs of the cycle. Objects carry exactly the keys above; there is no
free text beyond ids, titles, artists, key names and roman numerals (never lyrics).
``validate`` checks all of it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .contract import FRAME, KEY_RE, QUALITIES, ROMAN_RE, SLUG_RE, _intervals, _keys, _num

VERSION = 1
KINDS = {"duet", "handoff"}
DOC_KEYS = {"version", "generated_at", "frame", "paths"}
PATH_KEYS = {"id", "title", "file", "seconds", "loops", "root", "key", "bpm", "beats_per_bar", "phrase_beats",
             "segments", "beats", "chords", "songs"}
SEG_KEYS = {"start", "end", "kind", "instrumental", "vocals", "entering", "leaving", "chord_match"}
SONG_KEYS = {"work_id", "title", "artist", "year", "step", "shift_semitones", "tempo_ratio", "melody",
             "vocal_audible", "instrumental_audible"}
EPS = 1e-3


def loop_file(path_id: str) -> str:
    return f"{path_id}/loop.mp3"


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _segments(p: dict, w: str, seconds: float, song_ids: list, err: list[str]) -> None:
    segs = p["segments"]
    if not isinstance(segs, list) or not segs:
        err.append(f"{w}: no segments")
        return
    prev_end = 0.0
    pairs = []
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
            continue
        voc = s["vocals"]
        if not (isinstance(voc, list) and len(voc) == 2 and all(v in song_ids for v in voc) and voc[0] != voc[1]):
            err.append(f"{ws}: vocals must be two different songs of the path")
            continue
        if s["instrumental"] not in song_ids:
            err.append(f"{ws}: instrumental not among the path's songs")
        c = s["chord_match"]
        if not (_num(c) and 0 <= c <= 1):
            err.append(f"{ws}: chord_match must be in 0..1")
        if s["kind"] == "duet":
            if s["entering"] is not None or s["leaving"] is not None:
                err.append(f"{ws}: a duet has no entering/leaving song")
            if s["instrumental"] != p["root"]:
                err.append(f"{ws}: a duet plays the root's instrumental")
            pairs.append(tuple(voc))
        else:
            ent, lea = s["entering"], s["leaving"]
            if ent not in voc or lea in voc or lea not in song_ids:
                err.append(f"{ws}: entering must be one of the vocals and leaving another song")
                continue
            want = lea if ent == p["root"] else ent
            if s["instrumental"] != want:
                err.append(f"{ws}: a handoff borrows the entering song's instrumental (the leaving one's into the root)")
    if abs(prev_end - seconds) > 0.05:
        err.append(f"{w}: segments end at {prev_end}, loop lasts {seconds}")
    # the cycle: duets are (S0,S1), (S1,S2), ..., (S(n-1),S0)
    n = len(song_ids)
    want = [(song_ids[k], song_ids[(k + 1) % n]) for k in range(n)]
    if pairs and pairs != want:
        err.append(f"{w}: duet pairs {pairs} do not follow the cycle {want}")


def _melody(mel, w: str, n_beats: float, err: list[str]) -> None:
    if not isinstance(mel, list):
        err.append(f"{w}: melody")
        return
    prev = None
    for mi, pt in enumerate(mel):
        if not (isinstance(pt, list) and len(pt) == 2 and _num(pt[0]) and (pt[1] is None or _num(pt[1]))):
            err.append(f"{w}: melody[{mi}] malformed")
            return
        if not 0 <= pt[0] < n_beats or (pt[1] is not None and not 20 <= pt[1] <= 110):
            err.append(f"{w}: melody[{mi}] = {pt} out of range")
            return
        if prev is not None and pt[0] < prev[0] - 1e-9:
            err.append(f"{w}: melody beats decrease at {mi}")
            return
        if prev is not None and pt[1] is not None and prev[1] is not None and pt[0] - prev[0] < 1 / 16 - 1e-6:
            err.append(f"{w}: melody denser than 1/16 beat at {mi}")
            return
        prev = pt


def validate(doc: dict, root: Path | None = None) -> list[str]:
    """Contract violations (empty = valid). With ``root`` (data/audio/duets) the files must exist."""
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
        if p["file"] != loop_file(str(pid)):
            err.append(f"{w}: file must be {loop_file(str(pid))!r}")
        elif root is not None and not (Path(root) / p["file"]).is_file():
            err.append(f"{w}: {p['file']} missing")
        if p["loops"] is not True:
            err.append(f"{w}: loops must be true")
        seconds = p["seconds"]
        if not _num(seconds) or seconds <= 0:
            err.append(f"{w}: seconds")
            continue
        if not (isinstance(p["key"], str) and KEY_RE.match(p["key"])):
            err.append(f"{w}: key {p['key']!r}")
        if not (_num(p["bpm"]) and 20 <= p["bpm"] <= 400):
            err.append(f"{w}: bpm")
        bpb = p["beats_per_bar"]
        if not _int(bpb) or not 2 <= bpb <= 12:
            err.append(f"{w}: beats_per_bar")
            bpb = 4
        pb = p["phrase_beats"]
        if not _num(pb) or pb <= 0 or abs(pb / bpb - round(pb / bpb)) > EPS:
            err.append(f"{w}: phrase_beats must be a positive whole number of bars")
        songs = p["songs"]
        if not isinstance(songs, list) or len(songs) < 3:
            err.append(f"{w}: a duet loop needs at least three songs")
            continue
        song_ids = [s.get("work_id") if isinstance(s, dict) else None for s in songs]
        if len(set(song_ids)) != len(song_ids):
            err.append(f"{w}: duplicate songs")
        if not song_ids or p["root"] != song_ids[0]:
            err.append(f"{w}: root must be the first song")
        _segments(p, w, seconds, song_ids, err)
        # beats
        beats = p["beats"]
        n_beats = 0.0
        if not isinstance(beats, list) or len(beats) < 2:
            err.append(f"{w}: beats")
        else:
            last = -1.0
            for bi, b in enumerate(beats):
                if not (isinstance(b, list) and len(b) == 2 and _num(b[0]) and _num(b[1])):
                    err.append(f"{w}: beats[{bi}] malformed")
                    break
                if b[0] <= last or b[0] < 0 or b[0] >= seconds or abs(b[1] - bi) > EPS:
                    err.append(f"{w}: beats[{bi}] = {b} out of order or range")
                    break
                last = b[0]
            n_beats = float(len(beats))
            if n_beats % bpb:
                err.append(f"{w}: {len(beats)} beats is not a whole number of bars")
        # chords
        chords = p["chords"]
        if not isinstance(chords, list) or not chords:
            err.append(f"{w}: chords")
        else:
            prev_end = 0.0
            for ci, ch in enumerate(chords):
                if not (isinstance(ch, list) and len(ch) == 5 and _num(ch[0]) and _num(ch[1]) and _int(ch[2])
                        and ch[3] in QUALITIES and isinstance(ch[4], str) and ROMAN_RE.match(ch[4])):
                    err.append(f"{w}: chords[{ci}] malformed: {ch}")
                    break
                if not (0 <= ch[0] < ch[1] <= n_beats + EPS and 0 <= ch[2] <= 11) or ch[0] < prev_end - EPS:
                    err.append(f"{w}: chords[{ci}] out of range or order: {ch}")
                    break
                prev_end = ch[1]
        # songs
        for k, so in enumerate(songs):
            wo = f"{w} song {k}"
            if not _keys(so, SONG_KEYS, wo, err):
                continue
            if so["step"] != k:
                err.append(f"{wo}: step {so['step']} != {k}")
            for f in ("work_id", "title", "artist"):
                if not isinstance(so[f], str) or not so[f]:
                    err.append(f"{wo}: {f}")
            if not _int(so["year"]):
                err.append(f"{wo}: year")
            if not (_int(so["shift_semitones"]) and -6 <= so["shift_semitones"] <= 6):
                err.append(f"{wo}: shift_semitones")
            if k == 0 and so["shift_semitones"] != 0:
                err.append(f"{wo}: the root is not transposed")
            if not (_num(so["tempo_ratio"]) and 0.25 <= so["tempo_ratio"] <= 4):
                err.append(f"{wo}: tempo_ratio")
            _melody(so["melody"], wo, n_beats or 1e9, err)
            _intervals(so["vocal_audible"], seconds, f"{wo} vocal_audible", err)
            _intervals(so["instrumental_audible"], seconds, f"{wo} instrumental_audible", err)
            if isinstance(so["vocal_audible"], list) and not so["vocal_audible"]:
                err.append(f"{wo}: never sings")
    return err
