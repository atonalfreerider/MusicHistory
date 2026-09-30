"""Chord sequences in the normalized key frame, with the token encodings of ``db.py``.

    L1 token = root * 3 + q    q: 0 maj ('', 7, maj7), 1 min (m, m7), 2 dim      36 symbols
    L2 token = root * 6 + q    q: 0 '', 1 m, 2 7, 3 maj7, 4 m7, 5 dim             72 symbols
    cd       = token * 8 + clip(round(log2(beats)), -1, 4) + 1
    keyfree  = ((root_b - root_a) % 12) * 9 + qa * 3 + qb     (L1 q, from chg)   108 symbols

Kinds (DESIGN.md §7, mir-methods.md §2.2):

* ``chg``: changes. Rests dropped; chords shorter than 0.75 beat (passing chords, the same
  threshold Resonance's RegionPhases uses) are absorbed by the chord they follow; equal
  neighbours collapse (across rests). Robust to arrangement and transcription detail.
* ``cd``: chg tokens with a duration class (harmonic rhythm), more specific evidence.
* ``beat``: one token per quarter-note beat, the chord covering most of it (-1 none).
* ``keyfree``: interval + qualities of each change; immune to key errors (L1 only, as the
  encoding is defined on L1 qualities).

L1 is the primary level (de Haas et al. 2011: triads beat simpler and richer vocabularies).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .meter import Meter

L1_Q = {"": 0, "7": 0, "maj7": 0, "m": 1, "m7": 1, "dim": 2}
L2_Q = {"": 0, "m": 1, "7": 2, "maj7": 3, "m7": 4, "dim": 5}
PASSING_BEATS = 0.75
KINDS = (("chg", "L1"), ("chg", "L2"), ("cd", "L1"), ("cd", "L2"), ("beat", "L1"), ("beat", "L2"), ("keyfree", "L1"))

Chord = tuple[float, float, int, str]  # (start, end, root C = 0 or -1, quality)


def l1(root: int, quality: str) -> int:
    return root * 3 + L1_Q.get(quality, 0)


def l2(root: int, quality: str) -> int:
    return root * 6 + L2_Q.get(quality, 0)


def l1_parts(token: int) -> tuple[int, int]:
    return token // 3, token % 3


def dur_class(beats: float) -> int:
    """0..5 for 1/2, 1, 2, 4, 8, 16+ beats (round half up, so C# ports agree)."""
    if beats <= 0:
        return 0
    return min(4, max(-1, math.floor(math.log2(beats) + 0.5))) + 1


def cd_token(token: int, beats: float) -> int:
    return token * 8 + dur_class(beats)


def keyfree_token(a: int, b: int) -> int:
    (ra, qa), (rb, qb) = l1_parts(a), l1_parts(b)
    return ((rb - ra) % 12) * 9 + qa * 3 + qb


@dataclass
class ChordSeq:
    tokens: list[int]
    starts: list[float]
    durs: list[float]
    downbeat: list[int]

    def __len__(self) -> int:
        return len(self.tokens)


def normalize(chords: list, shift_at: Callable[[float], int]) -> list[Chord]:
    """Slim chord rows transposed by the shift of the key region at each chord's start."""
    out: list[Chord] = []
    for start, end, root, quality in chords:
        if end <= start:
            continue
        r = -1 if root < 0 else (int(root) + shift_at(float(start))) % 12
        out.append((float(start), float(end), r, quality if r >= 0 else ""))
    return out


def changes(chords: list[Chord], level: str, meter: Meter) -> ChordSeq:
    enc = l1 if level == "L1" else l2
    rows: list[list] = []  # [token, start, end]
    for start, end, root, quality in chords:
        if root < 0:
            continue
        tok = enc(root, quality)
        if end - start < PASSING_BEATS:
            if rows and abs(rows[-1][2] - start) < 1e-6:
                rows[-1][2] = end   # a passing chord belongs to the chord it follows
            continue
        if rows and rows[-1][0] == tok:
            rows[-1][2] = end
            continue
        rows.append([tok, start, end])
    return ChordSeq([r[0] for r in rows], [round(r[1], 4) for r in rows], [round(r[2] - r[1], 4) for r in rows],
                    [int(meter.is_downbeat(r[1])) for r in rows])


def with_durations(chg: ChordSeq) -> ChordSeq:
    return ChordSeq([cd_token(t, d) for t, d in zip(chg.tokens, chg.durs)], list(chg.starts), list(chg.durs),
                    list(chg.downbeat))


def per_beat(chords: list[Chord], level: str, end_beat: float, meter: Meter) -> ChordSeq:
    enc = l1 if level == "L1" else l2
    n = max(0, math.ceil(end_beat - 1e-6))
    cover: list[dict[int, float]] = [{} for _ in range(n)]
    for start, end, root, quality in chords:
        tok = -1 if root < 0 else enc(root, quality)
        for b in range(max(0, math.floor(start)), min(n, math.ceil(end))):
            ov = min(end, b + 1) - max(start, b)
            if ov > 1e-9:
                cover[b][tok] = cover[b].get(tok, 0.0) + ov
    tokens = [max(c.items(), key=lambda kv: kv[1])[0] if c else -1 for c in cover]
    return ChordSeq(tokens, [float(b) for b in range(n)], [1.0] * n, [int(meter.is_downbeat(b)) for b in range(n)])


def keyfree(chg_l1: ChordSeq) -> ChordSeq:
    t = chg_l1.tokens
    return ChordSeq([keyfree_token(a, b) for a, b in zip(t, t[1:])], chg_l1.starts[1:], chg_l1.durs[1:],
                    chg_l1.downbeat[1:])


def sequences(chords: list[Chord], end_beat: float, meter: Meter) -> dict[tuple[str, str], ChordSeq]:
    """Every (kind, level) the chord_seq table stores."""
    out: dict[tuple[str, str], ChordSeq] = {}
    for level in ("L1", "L2"):
        chg = changes(chords, level, meter)
        out[("chg", level)] = chg
        out[("cd", level)] = with_durations(chg)
        out[("beat", level)] = per_beat(chords, level, end_beat, meter)
    out[("keyfree", "L1")] = keyfree(out[("chg", "L1")])
    return out
