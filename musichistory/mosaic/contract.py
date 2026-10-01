"""``data/audio/mosaics/mosaics.json`` (version 1): the contract between the ``mosaic`` stage and
the Unity viewer's mosaic player and melody graph (DESIGN §17). Shape (UTF-8)::

    { "version": 1, "generated_at": ISO-8601,
      "frame": "C major / A minor (relative normalization, as the pipeline)",
      "mosaics": [ { "id": slug, "name": short Title Case name (<= 32 characters),
                     "title": "<target title> rebuilt from <n> songs",
                     "target": { "work_id", "title", "artist", "year" },
                     "file": "<id>/mix.mp3" (relative to data/audio/mosaics), "seconds",
                     "key": the target's key ("C major"), "bpm", "beats_per_bar": int,
                     "loop_beats": whole bars of beats,
                     "sections": [ { "start", "end" (seconds; contiguous from 0 to seconds),
                                     "kind": "original" | "mosaic" | "harmony" (in this order),
                                     "loops": int } ],
                     "beats": [ [mix seconds, loop beat], ... ]  (every beat; 0 <= loop beat < loop_beats),
                     "chords": [ [start beat, end beat, root pc 0..11, quality, roman], ... ] (the loop's),
                     "notes": [ [start beat, end beat, MIDI pitch], ... ]  (the target loop's melody),
                     "pieces": [ { "work_id", "title", "artist", "year",
                                   "start", "end" (loop beats it covers),
                                   "source_start", "source_end" (preview seconds),
                                   "shift_semitones": int -6..6, "tempo_ratio" (0.66..1.5),
                                   "match": 0..1, "notes": [ [start, end, pitch], ... ] (as heard) } ],
                     "harmonies": [ { "work_id", "title", "artist", "year", "shift_semitones",
                                      "tempo_ratio", "consonance": 0..1, "notes": [...] } ] (1 or 2),
                     "coverage": 0..1, "match": 0..1 } ] }

Beats, chords and notes are in loop beats (0 .. loop_beats) and every pitch and chord root in
the normalized C major / A minor frame of the target, as heard. Pieces are sorted, do not
overlap and come from songs other than the target; ``match`` (the share of the loop's notes the
pieces sing note for note) is at most ``coverage`` (the share inside the pieces' spans). Each
section lasts its ``loops`` whole loops. Objects carry exactly the keys above; there is no free
text beyond ids, names, titles, artists, key names and roman numerals (never lyrics).
``validate`` checks all of it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..mashup.contract import FRAME, KEY_RE, QUALITIES, ROMAN_RE, SLUG_RE, _keys, _num

VERSION = 1
KINDS = ("original", "mosaic", "harmony")
DOC_KEYS = {"version", "generated_at", "frame", "mosaics"}
MOSAIC_KEYS = {"id", "name", "title", "target", "file", "seconds", "key", "bpm", "beats_per_bar", "loop_beats",
               "sections", "beats", "chords", "notes", "pieces", "harmonies", "coverage", "match"}
TARGET_KEYS = {"work_id", "title", "artist", "year"}
SECTION_KEYS = {"start", "end", "kind", "loops"}
PIECE_KEYS = {"work_id", "title", "artist", "year", "start", "end", "source_start", "source_end",
              "shift_semitones", "tempo_ratio", "match", "notes"}
HARMONY_KEYS = {"work_id", "title", "artist", "year", "shift_semitones", "tempo_ratio", "consonance", "notes"}
NAME_MAX = 32
MAX_SECONDS = 90.0
TEMPO_RANGE = (0.66, 1.5)
EPS = 1e-3


def mix_file(mosaic_id: str) -> str:
    return f"{mosaic_id}/mix.mp3"


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _song(o: dict, w: str, err: list[str]) -> None:
    for f in ("work_id", "title", "artist"):
        if not isinstance(o[f], str) or not o[f]:
            err.append(f"{w}: {f}")
    if not _int(o["year"]):
        err.append(f"{w}: year")


def _notes(v, w: str, loop_beats: float, err: list[str]) -> None:
    if not isinstance(v, list):
        err.append(f"{w}: notes is not a list")
        return
    last = -1.0
    for k, n in enumerate(v):
        if not (isinstance(n, list) and len(n) == 3 and all(_num(x) for x in n)):
            err.append(f"{w}: notes[{k}] malformed")
            return
        a, b, p = n
        if not (0 <= a < b <= loop_beats + EPS and 20 <= p <= 110) or a < last - EPS:
            err.append(f"{w}: notes[{k}] = {n} out of range or order")
            return
        last = a


def _shift_tempo(o: dict, w: str, err: list[str]) -> None:
    if not (_int(o["shift_semitones"]) and -6 <= o["shift_semitones"] <= 6):
        err.append(f"{w}: shift_semitones")
    lo, hi = TEMPO_RANGE
    if not (_num(o["tempo_ratio"]) and lo - 0.02 <= o["tempo_ratio"] <= hi + 0.02):
        err.append(f"{w}: tempo_ratio {o['tempo_ratio']!r} outside {lo}..{hi}")


def validate(doc: dict, root: Path | None = None) -> list[str]:
    """Contract violations (empty = valid). With ``root`` (data/audio/mosaics) the files must exist."""
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
    if not isinstance(doc["mosaics"], list):
        return err + ["mosaics is not a list"]
    ids = set()
    for mi, m in enumerate(doc["mosaics"]):
        w = f"mosaics[{mi}]"
        if not _keys(m, MOSAIC_KEYS, w, err):
            continue
        mid = m["id"]
        if not (isinstance(mid, str) and SLUG_RE.match(mid)) or mid in ids:
            err.append(f"{w}: bad or duplicate id {mid!r}")
        ids.add(mid)
        w = f"mosaic {mid}"
        name = m["name"]
        if not (isinstance(name, str) and 0 < len(name) <= NAME_MAX and name[0].isupper()):
            err.append(f"{w}: name must be Title Case, at most {NAME_MAX} characters")
        if not isinstance(m["title"], str) or not m["title"]:
            err.append(f"{w}: title")
        tgt = m["target"]
        if _keys(tgt, TARGET_KEYS, f"{w} target", err):
            _song(tgt, f"{w} target", err)
        target_id = tgt.get("work_id") if isinstance(tgt, dict) else None
        if m["file"] != mix_file(str(mid)):
            err.append(f"{w}: file must be {mix_file(str(mid))!r}")
        elif root is not None and not (Path(root) / m["file"]).is_file():
            err.append(f"{w}: {m['file']} missing")
        seconds = m["seconds"]
        if not (_num(seconds) and 0 < seconds <= MAX_SECONDS + EPS):
            err.append(f"{w}: seconds must be in (0, {MAX_SECONDS}]")
            continue
        if not (isinstance(m["key"], str) and KEY_RE.match(m["key"])):
            err.append(f"{w}: key {m['key']!r}")
        if not (_num(m["bpm"]) and 20 <= m["bpm"] <= 400):
            err.append(f"{w}: bpm")
        bpb = m["beats_per_bar"]
        if not _int(bpb) or not 2 <= bpb <= 12:
            err.append(f"{w}: beats_per_bar")
            bpb = 4
        lb = m["loop_beats"]
        if not (_num(lb) and lb > 0 and abs(lb / bpb - round(lb / bpb)) <= EPS):
            err.append(f"{w}: loop_beats must be a positive whole number of bars")
            continue
        # sections
        secs = m["sections"]
        n_loops = 0
        if not isinstance(secs, list) or [s.get("kind") if isinstance(s, dict) else None for s in secs] != list(KINDS):
            err.append(f"{w}: sections must be original, mosaic, harmony")
        else:
            prev = 0.0
            for si, s in enumerate(secs):
                ws = f"{w} section {si}"
                if not _keys(s, SECTION_KEYS, ws, err):
                    continue
                if not (_num(s["start"]) and _num(s["end"]) and s["start"] < s["end"]):
                    err.append(f"{ws}: start/end")
                    continue
                if abs(s["start"] - prev) > EPS:
                    err.append(f"{ws}: starts at {s['start']}, previous ended at {prev}")
                prev = s["end"]
                if not (_int(s["loops"]) and s["loops"] >= 1):
                    err.append(f"{ws}: loops must be a positive int")
                    continue
                n_loops += s["loops"]
            if abs(prev - seconds) > 0.05:
                err.append(f"{w}: sections end at {prev}, mix lasts {seconds}")
            if n_loops:
                loop_s = seconds / n_loops
                for si, s in enumerate(secs):
                    if isinstance(s, dict) and _int(s.get("loops")) and _num(s.get("start")) and _num(s.get("end")) \
                            and abs((s["end"] - s["start"]) - s["loops"] * loop_s) > 0.05:
                        err.append(f"{w} section {si}: not {s['loops']} whole loops")
                if seconds + loop_s <= MAX_SECONDS - EPS:
                    err.append(f"{w}: another whole loop would fit in {MAX_SECONDS:g} s")
        # beats
        beats = m["beats"]
        if not isinstance(beats, list) or len(beats) < 2:
            err.append(f"{w}: beats")
        else:
            last = -1.0
            for bi, b in enumerate(beats):
                if not (isinstance(b, list) and len(b) == 2 and _num(b[0]) and _num(b[1])):
                    err.append(f"{w}: beats[{bi}] malformed")
                    break
                if b[0] <= last or b[0] < 0 or b[0] >= seconds or not 0 <= b[1] < lb or abs(b[1] - bi % lb) > EPS:
                    err.append(f"{w}: beats[{bi}] = {b} out of order or range")
                    break
                last = b[0]
            if n_loops and len(beats) != round(n_loops * lb):
                err.append(f"{w}: {len(beats)} beats, expected {round(n_loops * lb)}")
        # chords
        chords = m["chords"]
        if not isinstance(chords, list) or not chords:
            err.append(f"{w}: chords")
        else:
            prev = 0.0
            for ci, ch in enumerate(chords):
                if not (isinstance(ch, list) and len(ch) == 5 and _num(ch[0]) and _num(ch[1]) and _int(ch[2])
                        and ch[3] in QUALITIES and isinstance(ch[4], str) and ROMAN_RE.match(ch[4])):
                    err.append(f"{w}: chords[{ci}] malformed: {ch}")
                    break
                if not (0 <= ch[0] < ch[1] <= lb + EPS and 0 <= ch[2] <= 11) or ch[0] < prev - EPS:
                    err.append(f"{w}: chords[{ci}] out of range or order: {ch}")
                    break
                prev = ch[1]
        _notes(m["notes"], f"{w} notes", lb, err)
        if isinstance(m["notes"], list) and not m["notes"]:
            err.append(f"{w}: the target loop has no notes")
        # pieces
        pieces = m["pieces"]
        if not isinstance(pieces, list) or not pieces:
            err.append(f"{w}: no pieces")
            pieces = []
        prev_end = 0.0
        for pi, p in enumerate(pieces):
            wp = f"{w} piece {pi}"
            if not _keys(p, PIECE_KEYS, wp, err):
                continue
            _song(p, wp, err)
            if p["work_id"] == target_id:
                err.append(f"{wp}: a piece must come from another song")
            if not (_num(p["start"]) and _num(p["end"]) and 0 <= p["start"] < p["end"] <= lb + EPS):
                err.append(f"{wp}: start/end")
            elif p["start"] < prev_end - EPS:
                err.append(f"{wp}: overlaps the previous piece or is out of order")
            else:
                prev_end = p["end"]
            if not (_num(p["source_start"]) and _num(p["source_end"]) and 0 <= p["source_start"] < p["source_end"]):
                err.append(f"{wp}: source_start/source_end")
            _shift_tempo(p, wp, err)
            if not (_num(p["match"]) and 0 <= p["match"] <= 1):
                err.append(f"{wp}: match")
            _notes(p["notes"], wp, lb, err)
        # harmonies
        harm = m["harmonies"]
        if not isinstance(harm, list) or not 1 <= len(harm) <= 2:
            err.append(f"{w}: one or two harmonies")
            harm = harm if isinstance(harm, list) else []
        seen = set()
        for hi_, h in enumerate(harm):
            wh = f"{w} harmony {hi_}"
            if not _keys(h, HARMONY_KEYS, wh, err):
                continue
            _song(h, wh, err)
            if h["work_id"] == target_id or h["work_id"] in seen:
                err.append(f"{wh}: must be another song than the target and the other voice")
            seen.add(h["work_id"])
            _shift_tempo(h, wh, err)
            if not (_num(h["consonance"]) and 0 <= h["consonance"] <= 1):
                err.append(f"{wh}: consonance")
            _notes(h["notes"], wh, lb, err)
        cov, mat = m["coverage"], m["match"]
        if not (_num(cov) and 0 <= cov <= 1 and _num(mat) and 0 <= mat <= 1):
            err.append(f"{w}: coverage/match must be in 0..1")
        elif mat > cov + EPS:
            err.append(f"{w}: match {mat} exceeds coverage {cov}")
    return err
