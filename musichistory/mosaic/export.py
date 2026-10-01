"""One mosaic's entry of ``mosaics.json`` (see ``contract.py``) and its names.

**Normalized frame.** Everything is heard in the target's key, so one offset serves the whole
mix: ``F`` = the target's relative-normalization shift (C major / A minor, within a tritone,
``mashup.export.wrap_offset``). Notes are tuning-corrected MIDI pitches of their recordings; a
piece or harmony voice is heard transposed by its ``shift`` (and tuned to the target), so its
note ``p`` is heard at ``p + shift`` and lands at ``p + shift - F`` in the frame; the target's
own notes at ``p - F``.

**Chords** are the loop's half-bar chords (``mashup.tracks.Track.bar_chords``), merged where
consecutive slots agree, roots in the frame, roman numerals in the target's mode.

**Names.** ``name``: "<Title>, Reassembled" in Title Case when it fits in 32 characters (else
"<Title> Rebuilt", else the title's first words); ``title``: "<Title> rebuilt from <n> songs";
``id``: the slug of the short title plus ``-mosaic``.
"""

from __future__ import annotations

import re

import numpy as np

from ..mashup.export import roman, wrap_offset
from ..paths.identity import frame_shift
from .contract import NAME_MAX, mix_file
from .render import Rendered, mix_beats

SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs"}


def frame_offset(tonic: int, mode: str) -> int:
    return wrap_offset(frame_shift(tonic, mode))


def short_title(title: str) -> str:
    """The title without parenthesized or bracketed parts and featured credits."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title)
    t = re.split(r"\s+(?:feat\.?|featuring|ft\.)\s+", t, flags=re.I)[0]
    return re.sub(r"\s+", " ", t).strip(" -,") or title.strip()


def title_case(s: str) -> str:
    words = s.split(" ")
    out = []
    for i, w in enumerate(words):
        if not w:
            continue
        if 0 < i < len(words) - 1 and w.lower() in SMALL:
            out.append(w.lower())
        else:
            out.append(w[0].upper() + w[1:])
    return " ".join(out)


def names(title: str, n_songs: int) -> tuple[str, str, str]:
    """(id, name, title) of a mosaic of ``title``."""
    short = title_case(short_title(title))
    for cand in (f"{short}, Reassembled", f"{short} Rebuilt", short):
        if len(cand) <= NAME_MAX:
            name = cand
            break
    else:
        words, name = short.split(), ""
        for w in words:
            if len(f"{name} {w}".strip()) > NAME_MAX:
                break
            name = f"{name} {w}".strip()
        name = name or short[:NAME_MAX]
    slug = re.sub(r"[^a-z0-9]+", "-", short_title(title).lower().replace("'", "")).strip("-") or "song"
    return f"{slug}-mosaic", name, f"{short_title(title)} rebuilt from {n_songs} songs"


def chords(slot_chords: list[list[int]], bpb: int, f0: int, mode: str) -> list[list]:
    out: list[list] = []
    for k, labs in enumerate(slot_chords):
        n = len(labs)
        for i, lab in enumerate(labs):
            if lab < 0:
                continue
            root = (lab // 2 - f0) % 12
            minor = bool(lab % 2)
            q = "min" if minor else "maj"
            a, b = k * bpb + i * bpb / n, k * bpb + (i + 1) * bpb / n
            if out and abs(out[-1][1] - a) < 1e-9 and out[-1][2] == root and out[-1][3] == q:
                out[-1][1] = b
                continue
            out.append([a, b, root, q, roman(root, minor, mode)])
    return [[float(a), float(b), int(r), q, rn] for a, b, r, q, rn in out]


def note_rows(on, off, pitch, loop_beats: int, offset: int) -> list[list]:
    """[start, end, pitch - offset] clipped to the loop (dropped when nothing is left)."""
    rows = []
    for a, b, p in zip(on, off, pitch):
        a, b = max(float(a), 0.0), min(float(b), float(loop_beats))
        if b - a > 1e-3:
            rows.append([round(a, 4), round(b, 4), int(p) - offset])
    rows.sort(key=lambda r: r[0])
    return rows


def mosaic_entry(*, mosaic_id: str, name: str, title: str, target: dict, key: str, f0: int, r: Rendered,
                 bpb: int, slot_chords: list[list[int]], mode: str, target_notes, pieces: list[dict],
                 harmonies: list[dict], coverage: float, match: float) -> dict:
    """``pieces``/``harmonies``: contract objects whose ``notes`` are still in recording pitches
    as heard (shift applied); they are moved into the frame here."""
    lb = r.grid.n_beats
    T = r.grid.seconds
    secs = [{"start": round(a * T, 3), "end": round((a + n) * T, 3), "kind": kind, "loops": int(n)}
            for kind, a, n in r.sections]
    secs[-1]["end"] = round(r.seconds, 3)
    beats = [[round(t, 3), float(q)] for t, q in mix_beats(r)]
    for p in pieces + harmonies:
        p["notes"] = [[a, b, int(x) - f0] for a, b, x in p["notes"]]
    return {"id": mosaic_id, "name": name, "title": title, "target": target, "file": mix_file(mosaic_id),
            "seconds": round(r.seconds, 3), "key": key, "bpm": round(60.0 / float(np.median(np.diff(r.grid.times))), 2),
            "beats_per_bar": int(bpb), "loop_beats": int(lb), "sections": secs, "beats": beats,
            "chords": chords(slot_chords, bpb, f0, mode),
            "notes": note_rows(target_notes.on, target_notes.off, target_notes.pitch, lb, f0),
            "pieces": pieces, "harmonies": harmonies, "coverage": round(float(coverage), 4),
            "match": round(float(match), 4)}
