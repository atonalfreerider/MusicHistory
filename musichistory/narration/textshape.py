"""Text shaping for downward inflection (DESIGN §15).

A narrator's pitch falls at the end of a declarative sentence that ends in a period; questions,
exclamations, trailing commas and ellipses invite a rising or suspended ending. ``shape`` turns
a cue's line into plain declarative sentences that each end in a period; ``sentences`` splits it
the way the inflection check counts sentence ends.
"""

from __future__ import annotations

import re

# Abbreviations whose period does not end a sentence.
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "no", "vol", "feat", "approx", "ca",
    "e.g", "i.e", "u.s", "u.k", "a.m", "p.m", "mt", "ft", "co", "inc", "ltd", "bros",
}
_CLOSERS = "\"')]”’"
_TERMINAL_RE = re.compile(r"\.[\"')\]”’]*$")


def shape(text: str) -> str:
    """The line as declarative sentences ending in periods: no ``?`` or ``!``, no ellipses, no
    dangling comma/colon/dash at the end, single spaces, and a final period."""
    t = str(text).replace("…", ".")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\.{2,}(?=\s*[a-z])", ",", t)           # a mid-sentence ellipsis is a comma pause
    t = re.sub(r"[?!]+(?=\s*[a-z])", ",", t)            # ...and so is a mid-sentence ? or !
    t = re.sub(r"[?!.]*[?!][?!.]*", ".", t)             # questions and exclamations become statements
    t = re.sub(r"\.{2,}", ".", t)                       # ellipses and doubled periods
    t = re.sub(r"\s+([.,;:])", r"\1", t)                # no space before punctuation
    t = re.sub(r"([;:,])(?=[A-Za-z\"“])", r"\1 ", t)  # ...but one after it
    t = re.sub(r"[,;:\-–—\s]+$", "", t)        # dangling separators at the end
    if not t:
        return ""
    if not _TERMINAL_RE.search(t):
        t = t + "."
    return t


def is_shaped(text: str) -> bool:
    """True when ``text`` already obeys the shaping rules (the contract's caption check)."""
    return isinstance(text, str) and bool(text) and "?" not in text and "!" not in text \
        and bool(_TERMINAL_RE.search(text.strip()))


def sentences(text: str) -> list[str]:
    """Sentences of a shaped line: split after a period (optionally closed by a quote or bracket)
    that is followed by a space and a capital/digit/quote, except after an abbreviation, an
    initial ("J. S. Bach") or inside a decimal number."""
    t = text.strip()
    out, start = [], 0
    for m in re.finditer(rf"\.[{re.escape(_CLOSERS)}]*\s+(?=[A-Z0-9\"'“(])", t):
        before = t[start: m.start()]
        word = re.search(r"([A-Za-z.]+)$", before)
        w = word.group(1).lower() if word else ""
        if w.rstrip(".") in ABBREVIATIONS or (len(w) == 1 and w.isalpha()) or re.fullmatch(r"(?:[a-z]\.)+[a-z]", w):
            continue
        out.append(t[start: m.end()].strip())
        start = m.end()
    if t[start:].strip():
        out.append(t[start:].strip())
    return out


def words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?", text))
