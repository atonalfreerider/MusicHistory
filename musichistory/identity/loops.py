"""Rotation-invariant chord-loop identities from Resonance ``patterns`` (mir-methods.md §2.3).

A section family's consensus loop is cyclic, and where the segmentation starts it is an
accident (a 6/8 fixture came out as Am-F-G-C instead of C-Am-F-G). So a loop is reduced to:

1. L1 tokens in the normalized frame of the family's reference visit (its key region's
   shift plus its ``transpose``, since Resonance spells the loop in the first visit's key),
   rests dropped, passing chords (< 0.75 beat) absorbed, repeats collapsed, and the last
   chord merged into the first when they are equal (the loop wraps around);
2. its primitive period (C F C F -> C F), kept when it has 2..8 changes;
3. Booth's (1980) least rotation on the integer tokens -> ``cycle_id``; ``phase`` is the
   index in ``cycle_id`` where the song's own loop starts; ``rhythm_sig`` is the duration
   classes rotated the same way.

Only loops that actually repeat qualify (``passes >= 2`` or ``visits >= 2``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .chords import PASSING_BEATS, dur_class, l1, l1_parts

MAJOR_DEGREES = ["I", "bII", "II", "bIII", "III", "IV", "#IV", "V", "bVI", "VI", "bVII", "VII"]
MINOR_DEGREES = ["I", "bII", "II", "III", "#III", "IV", "#IV", "V", "VI", "#VI", "VII", "#VII"]
MIN_CHANGES, MAX_CHANGES = 2, 8


def booth(seq: list[int]) -> int:
    """Start index of the lexicographically least rotation (Booth 1980, O(n))."""
    s = list(seq) + list(seq)
    f = [-1] * len(s)
    k = 0
    for j in range(1, len(s)):
        sj = s[j]
        i = f[j - k - 1]
        while i != -1 and sj != s[k + i + 1]:
            if sj < s[k + i + 1]:
                k = j - i - 1
            i = f[i]
        if sj != s[k + i + 1]:  # here i == -1
            if sj < s[k]:
                k = j
            f[j - k] = -1
        else:
            f[j - k] = i + 1
    return k % max(1, len(seq))


def primitive_period(seq: list[int]) -> int:
    """Smallest p dividing len(seq) with seq == seq rotated by p."""
    n = len(seq)
    for p in range(1, n + 1):
        if n % p == 0 and seq[p:] + seq[:p] == seq:
            return p
    return n


def roman(token: int, frame_tonic: int = 0, minor: bool = False) -> str:
    """ASCII roman numeral of an L1 token relative to ``frame_tonic`` (C/Am frame)."""
    root, q = l1_parts(token)
    deg = (root - frame_tonic) % 12
    name = (MINOR_DEGREES if minor else MAJOR_DEGREES)[deg]
    acc = name.rstrip("IV")
    numeral = name[len(acc):]
    if q == 1:
        return acc + numeral.lower()
    if q == 2:
        return acc + numeral.lower() + "o"
    return acc + numeral


def roman_seq(tokens: list[int], frame_tonic: int = 0, minor: bool = False) -> str:
    return "-".join(roman(t, frame_tonic, minor) for t in tokens)


@dataclass
class Loop:
    family: int
    cycle_id: str
    phase: int
    rhythm_sig: str
    cycle_tokens: list[int]     # primitive cycle, song's own phase
    roman: str                  # major frame (C = I)
    roman_minor: str            # minor frame (A = i relative / C = i parallel)
    loop_beats: float
    passes: int
    visits: int
    coverage_beats: float
    visit_starts: list[float]


def reduce_loop(steps: list[tuple[float, float, int]]) -> tuple[list[int], list[float]] | None:
    """(L1 token, ...) + durations of the primitive cycle, or None if not a 2..8-change loop.
    ``steps`` are (start, end, token or -1) in loop order."""
    rows: list[list] = []
    for start, end, tok in steps:
        if tok < 0 or end <= start:
            continue
        if end - start < PASSING_BEATS:
            if rows:
                rows[-1][1] += end - start
            continue
        if rows and rows[-1][0] == tok:
            rows[-1][1] += end - start
        else:
            rows.append([tok, end - start])
    if len(rows) > 1 and rows[0][0] == rows[-1][0]:
        rows[0][1] += rows[-1][1]
        rows.pop()
    if not rows:
        return None
    toks = [r[0] for r in rows]
    p = primitive_period(toks)
    if not MIN_CHANGES <= p <= MAX_CHANGES:
        return None
    return toks[:p], [r[1] for r in rows[:p]]


def identity(tokens: list[int], durs: list[float]) -> tuple[str, int, str]:
    """(cycle_id, phase, rhythm_sig) of a primitive cycle in the song's own phase."""
    n = len(tokens)
    k = booth(tokens)
    cyc = tokens[k:] + tokens[:k]
    cls = [dur_class(d) for d in durs]
    rhy = cls[k:] + cls[:k]
    return ".".join(map(str, cyc)), (n - k) % n, ".".join(map(str, rhy))


def loop_shift(sections: list[dict], visits: list[dict], ref: int, shift_at: Callable[[float], int]) -> int:
    """Normalization shift for a family's pattern loop.

    Resonance spells the loop in the key of the family's *first* visit (FormPatterns writes
    ``Transpose(state, -T)`` with T relative to the first visit), while the reference is the
    earliest visit of the most common length, which may be a transposed later visit. The
    reference visit sounds at loop + T, so normalizing it with its own region's shift means
    loop + T + shift_ref: the same tokens as that visit's ``chg`` normalization."""
    if 0 <= ref < len(sections):
        s = sections[ref]
        return shift_at(float(s["start"])) + int(s.get("transpose", 0) or 0)
    return shift_at(float(visits[0]["start"])) if visits else shift_at(0.0)  # the first visit: T = 0


def from_slim(slim: dict, shift_at: Callable[[float], int], *, minor_frame_tonic: int = 9) -> list[Loop]:
    sections = slim.get("sections") or []
    by_family: dict[int, list[dict]] = {}
    for s in sections:
        by_family.setdefault(s["family"], []).append(s)
    out: list[Loop] = []
    for p in slim.get("patterns") or ():
        if max(p.get("passes", 0), p.get("visits", 0)) < 2:
            continue
        visits = by_family.get(p["family"], [])
        shift = loop_shift(sections, visits, p.get("reference", -1), shift_at)
        steps = [(float(c[0]), float(c[1]), -1 if c[2] < 0 else l1((int(c[2]) + shift) % 12, c[3])) for c in p["loop"]]
        reduced = reduce_loop(steps)
        if reduced is None:
            continue
        toks, durs = reduced
        cycle_id, phase, rhythm = identity(toks, durs)
        out.append(Loop(
            family=int(p["family"]), cycle_id=cycle_id, phase=phase, rhythm_sig=rhythm, cycle_tokens=toks,
            roman=roman_seq(toks), roman_minor=roman_seq(toks, minor_frame_tonic, True),
            loop_beats=round(sum(durs), 4), passes=int(p.get("passes", 0)), visits=int(p.get("visits", 0)),
            coverage_beats=round(sum(s["end"] - s["start"] for s in visits), 4),
            visit_starts=[round(float(s["start"]), 4) for s in visits]))
    return out


def main_loop(loops: list[Loop], minor: bool) -> str | None:
    """Display summary of the most-covering loop, e.g. 'vi-IV-I-V (i-VI-III-VII)'."""
    if not loops:
        return None
    best = max(loops, key=lambda lp: (lp.coverage_beats, lp.visits, -lp.family))
    return f"{best.roman} ({best.roman_minor})" if minor else best.roman
