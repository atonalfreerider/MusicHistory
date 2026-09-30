"""MIDI ingestion: validate raw bytes, sanitize to canonical SMF type 1, compute features.

    raw bytes --validate.parse--> RawMidi --sanitize.sanitize--> Sanitized(bytes, facts)
              --features.compute--> features_json dict

``process(data)`` runs all three and applies the final rejection rules (DESIGN.md §5).
No stage ever sees lyric or free text: text payloads are dropped while parsing and only
counts and tick positions survive.
"""

from __future__ import annotations

from dataclasses import dataclass

from .validate import InvalidMidi

MAX_BARS = 800
MIN_SECONDS = 30.0


@dataclass
class Processed:
    ok: bool
    reason: str | None       # invalid_reason slug when not ok
    data: bytes | None       # sanitized SMF bytes (None when parsing failed)
    features: dict | None


def process(data: bytes) -> Processed:
    """Validate, sanitize and describe one MIDI file (DESIGN.md §5 steps 1-7)."""
    from . import features, sanitize, validate

    try:
        raw = validate.parse(data)
        clean = sanitize.sanitize(raw)
        feats = features.compute(clean.data, clean.facts)
    except InvalidMidi as exc:
        return Processed(False, exc.reason, None, None)
    except Exception as exc:  # anything a hostile file can still trigger
        return Processed(False, f"unparseable:{type(exc).__name__}", None, None)
    reason = None
    if feats["n_notes"] == 0:
        reason = "no_notes"
    elif feats["n_bars"] > MAX_BARS:
        reason = "too_many_bars"
    elif feats["duration_s"] < MIN_SECONDS:
        reason = "too_short"
    return Processed(reason is None, reason, clean.data, feats)
